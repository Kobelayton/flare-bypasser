import typing
import copy
import json
import re
import httpx


"""
AsyncClient
httpx.AsyncClient wrapper for transient manipulations with sites and
transparent cloud flare protection bypassing.
"""


class AsyncClient(object):
  _solver_url = None
  _http_client: httpx.AsyncClient = None
  _args = []
  _kwargs = {}
  _user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
  # < base user-agent that will be used before first challenge solve,
  # after it will be replaced with solver actual user-agent
  _max_tries = 2

  class Exception(Exception):
    pass

  class CloudFlareBlocked(Exception):
    pass

  def __init__(self, solver_url, *args, **kwargs):
    self._solver_url = solver_url
    self._args = args
    self._kwargs = kwargs

  async def __aenter__(self):
    self._http_client = None  # < Cleanup previously opened connections
    self._init_client()
    await self._http_client.__aenter__()
    return self

  async def __aexit__(self, *args):
    if self._http_client:
      ret = await self._http_client.__aexit__(*args)
      self._http_client = None
      return ret
    return False

  @property
  def http_client(self) -> httpx.AsyncClient:
    return self._http_client

  async def get(self, url, *args, **kwargs) -> httpx.Response:
    return await self._request(httpx.AsyncClient.get, url, *args, **kwargs)

  async def post(self, url, *args, solve_url = None, **kwargs) -> httpx.Response:
    return await self._request(httpx.AsyncClient.post, url, *args, solve_url = solve_url, **kwargs)

  def _init_client(self):
    if not self._http_client:
      self._http_client = httpx.AsyncClient(http2 = True, *self._args, **self._kwargs)

  async def _request(self, run_method, url, *args, solve_url = None, headers = {}, **kwargs) -> httpx.Response:
    self._init_client()

    for try_i in range(self._max_tries):
      # request web page
      send_headers = copy.copy(headers)
      send_headers['user-agent'] = self._user_agent
      send_headers['cache-control'] = 'no-cache'  # < Disable cache, because httpx can return cached captcha response.
      response = await run_method(self._http_client, url, *args, headers = send_headers, **kwargs)

      if (
        response.status_code == 403 and
        response.headers.get('content-type', '').startswith('text/html') and
        response.text
      ):
        response_text = response.text.lower()

        # check that it is cloud flare unsolvable block
        if (
          (
            "access denied" in response_text and
            re.search(r'<\s*title\s*>\s*access denied\s[^><]*cloudflare[^><]*<\s*/\s*title\s*>', response_text)
          ) or
          (
            "ip banned" in response_text and "cloudflare" in response_text and
            re.search(r'<\s*title\s*>\s*ip banned[^><]*<\s*/\s*title\s*>', response_text)
          )
        ):
          raise AsyncClient.CloudFlareBlocked("IP blocked by cloud flare")

        # check that it is cloud flare block
        if (
            (
              "just a moment..." in response_text and
              re.search(r'<\s*title\s*>[^><]*just a moment\.\.\.[^><]*<\s*/\s*title\s*>', response_text)
            ) or
            (
              "attention required!" in response_text and
              re.search(r'<\s*title\s*>[^><]*attention required\s*![^><]*<\s*/\s*title\s*>', response_text)
            ) or
            (
              "captcha challenge" in response_text and
              re.search(r'<\s*title\s*>[^><]*captcha challenge[^><]*<\s*/\s*title\s*>', response_text)
            ) or
            (
              "ddos-guard" in response_text and
              re.search(r'<\s*title\s*>[^><]*ddos-guard[^><]*<\s*/\s*title\s*>', response_text)
            )):
          await self._solve_challenge(url if not solve_url else solve_url)
          continue  # < Repeat request with cf cookies

      return response

    raise AsyncClient.Exception(
      "Can't solve challenge: challenge got " + str(self._max_tries) + " times ... (max tries exceded)"
    )

  async def _solve_challenge(self, url):
    async with httpx.AsyncClient(http2 = False) as solver_client:
      solve_send_cookies = []
      if self._http_client:
        for c in self._http_client.cookies.jar:
          # c is http.cookiejar.Cookie
          solve_send_cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "port": c.port,
            "secure": c.secure,
            "expires": c.expires
          })
      solver_request = {
        "maxTimeout": 60000,
        "url": url,
        "cookies": solve_send_cookies,
        # < use for solve original client cookies,
        # it can contains some required information other that cloud flare marker.
        "proxy": self._kwargs.get('proxy', None),
      }
      solver_response = await solver_client.post(
        self._solver_url + '/get_cookies',
        headers={
          'Content-Type': 'application/json'
        },
        json=solver_request,
        timeout=61.0
      )
      if solver_response.status_code != 200:
        raise AsyncClient.Exception("Solver is unavailable: status_code = " + str(solver_response.status_code))

      response_json = solver_response.json()
      if "solution" not in response_json:
        raise AsyncClient.Exception(
          "Can't solve challenge: no solution in response for '" + str(url) + "': " +
          "response: " + json.dumps(response_json) +
          " on request: " + json.dumps(solver_request)
        )

      response_solution_json = response_json["solution"]
      self._user_agent = response_solution_json['userAgent']
      # Update _http_client cookies
      solver_cookies: typing.List[dict] = response_solution_json['cookies']
      for c in solver_cookies:
        self._http_client.cookies.set(
          name=c['name'],
          value=c['value'],
          domain=c.get('domain', ""),
          path=c.get('path', '/')
        )
