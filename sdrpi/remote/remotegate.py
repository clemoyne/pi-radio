#!/usr/bin/env python3
"""Gate between the Cloudflare tunnel and both Pis' pages.

Every request must carry a valid Cloudflare Access token. Anyone Access lets in may read
(GET and HEAD); anything else, and live listening, which takes over the radio, is for
owners only. The pages link to each other by .local names, which only work at home, so
text replies have them swapped for the public names.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import jwt
from aiohttp import ClientSession, ClientTimeout, TCPConnector, ThreadedResolver, WSMsgType, web
from multidict import CIMultiDict
from yarl import URL

CONFIG = Path(os.environ.get("REMOTEGATE_CONFIG", "/mnt/data/remotegate/config.json"))
PORT = int(os.environ.get("REMOTEGATE_PORT", "8097"))
READ_METHODS = {"GET", "HEAD"}
OWNER_ONLY_PATHS = {"/ws/listen"}  # a GET, but it retunes the HackRF
TEXT_TYPES = ("text/", "application/json", "application/javascript")
# Not passed on: hop-by-hop headers, Host (the upstream's own is used) and Accept-Encoding,
# so text arrives uncompressed and the .local names can be swapped
DROP_REQUEST = {"connection", "keep-alive", "proxy-authorization", "te", "trailer", "transfer-encoding",
                "upgrade", "host", "content-length", "accept-encoding"}
DROP_REPLY = {"connection", "keep-alive", "proxy-authenticate", "te", "trailer", "transfer-encoding",
              "upgrade", "content-length"}
DENIED = {"error": "Only the owner can do that from outside the house"}


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, file=sys.stderr, flush=True)


class Gate:
    """config.json: team (https://<team>.cloudflareaccess.com), aud (the Access application's
    audience tag), owners (emails), sites {public host: {upstream, local, also {path prefix: upstream}}}."""

    def __init__(self, cfg):
        self.team, self.aud = cfg["team"].rstrip("/"), cfg["aud"]
        self.owners = {e.lower() for e in cfg["owners"]}
        self.sites = cfg["sites"]
        self.keys = jwt.PyJWKClient(self.team + "/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)
        self.swaps = [(s["local"].encode(), ("https://" + host).encode()) for host, s in self.sites.items()]
        self.session = None

    def email(self, token):
        key = self.keys.get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.aud, issuer=self.team)
        return claims.get("email", "").lower()

    def swap(self, data):
        for local, public in self.swaps:
            data = data.replace(local, public)
        return data

    def upstream(self, site, path):
        for prefix, upstream in site.get("also", {}).items():
            if path.startswith(prefix):
                return upstream
        return site["upstream"]

    async def handle(self, request):
        site = self.sites.get(request.host.split(":")[0].lower())
        if not site:
            raise web.HTTPNotFound()
        try:
            email = await asyncio.to_thread(self.email, request.headers.get("Cf-Access-Jwt-Assertion", ""))
        except Exception as e:  # missing, expired or forged token, or the keys couldn't be fetched
            log("refused %s %s%s: %s" % (request.method, request.host, request.path, e))
            return web.json_response({"error": "Not signed in"}, status=403)
        if email not in self.owners and (request.method not in READ_METHODS or request.path in OWNER_ONLY_PATHS):
            log("read only: %s %s %s%s" % (email, request.method, request.host, request.path))
            return web.json_response(DENIED, status=403)
        url = URL(self.upstream(site, request.path) + request.raw_path, encoded=True)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.socket(request, url.with_scheme("ws"))
        return await self.forward(request, url)

    async def forward(self, request, url):
        headers = CIMultiDict((k, v) for k, v in request.headers.items() if k.lower() not in DROP_REQUEST)
        headers["X-Forwarded-Host"] = request.host
        body = request.content if request.body_exists else None
        async with self.session.request(request.method, url, headers=headers, data=body,
                                        allow_redirects=False) as up:
            reply = CIMultiDict((k, v) for k, v in up.headers.items() if k.lower() not in DROP_REPLY)
            if "Location" in reply:
                reply["Location"] = self.swap(reply["Location"].encode()).decode()
            if up.content_type.startswith(TEXT_TYPES) and "Content-Encoding" not in reply:
                return web.Response(status=up.status, body=self.swap(await up.read()), headers=reply)
            resp = web.StreamResponse(status=up.status, headers=reply)
            if up.content_length is not None:
                resp.content_length = up.content_length
            await resp.prepare(request)
            async for chunk in up.content.iter_chunked(65536):
                await resp.write(chunk)
            await resp.write_eof()
            return resp

    async def socket(self, request, url):
        ours = web.WebSocketResponse(heartbeat=30)
        await ours.prepare(request)
        headers = {k: v for k, v in request.headers.items() if k.lower() in ("cookie", "user-agent")}
        async with self.session.ws_connect(url, headers=headers, heartbeat=30) as theirs:
            async def pump(src, dst):
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    else:
                        break

            pumps = [asyncio.create_task(pump(ours, theirs)), asyncio.create_task(pump(theirs, ours))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in pumps:
                task.cancel()
            await theirs.close()
        await ours.close()
        return ours


async def on_start(app):
    # aiodns can't resolve .local names; the system resolver can (mDNS)
    app["gate"].session = ClientSession(connector=TCPConnector(resolver=ThreadedResolver()), auto_decompress=False,
                                        timeout=ClientTimeout(total=None, sock_connect=10))


async def on_stop(app):
    await app["gate"].session.close()


def main():
    gate = Gate(json.loads(CONFIG.read_text()))
    app = web.Application(client_max_size=0)
    app["gate"] = gate
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    app.router.add_route("*", "/{tail:.*}", gate.handle)
    log("listening on 127.0.0.1:%d for %s" % (PORT, ", ".join(gate.sites)))
    web.run_app(app, host="127.0.0.1", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
