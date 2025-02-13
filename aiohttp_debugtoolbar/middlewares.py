import sys

import aiohttp_jinja2
from aiohttp import web
from aiohttp.typedefs import Handler
from aiohttp.web_exceptions import _HTTPMove as HTTPMove

from .tbtools.tbtools import get_traceback
from .toolbar import DebugToolbar
from .utils import (
    APP_KEY,
    ContextSwitcher,
    REDIRECT_CODES,
    TEMPLATE_KEY,
    addr_in,
    hexlify,
)

__all__ = ("middleware",)
HTML_TYPES = ("text/html", "application/xhtml+xml")


@web.middleware
async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    app = request.app

    if APP_KEY not in app:
        raise RuntimeError(
            "Please setup debug toolbar with " "aiohttp_debugtoolbar.setup method"
        )

    # just create namespace for handler
    settings = app[APP_KEY]["settings"]
    request_history = app[APP_KEY]["request_history"]
    exc_history = app[APP_KEY]["exc_history"]
    intercept_exc = app[APP_KEY]["settings"]["intercept_exc"]

    if not app[APP_KEY]["settings"]["enabled"]:
        return await handler(request)

    # request['exc_history'] = exc_history
    panel_classes = settings.get("panels", ()) + settings.get("extra_panels", ())
    global_panel_classes = settings.get("global_panels", ())
    hosts = settings.get("hosts", [])

    show_on_exc_only = settings.get("show_on_exc_only")
    intercept_redirects = settings["intercept_redirects"]

    root_url = app.router["debugtoolbar.main"].url_for().raw_path
    exclude_prefixes = settings.get("exclude_prefixes", ())
    exclude = (root_url,) + exclude_prefixes

    p = request.raw_path
    starts_with_excluded = list(filter(None, map(p.startswith, exclude)))

    # FIXME: error when run trough unixsocket
    if request.transport:
        peername = request.transport.get_extra_info("peername")
        last_proxy_addr = peername[0]
    else:
        last_proxy_addr = ""

    # TODO: rethink access policy by host
    if settings.get("check_host"):
        if starts_with_excluded or not addr_in(last_proxy_addr, hosts):
            return await handler(request)

    toolbar = DebugToolbar(request, panel_classes, global_panel_classes)
    _handler = handler

    context_switcher = ContextSwitcher()
    for panel in toolbar.panels:
        _handler = panel.wrap_handler(_handler, context_switcher)

    try:
        response = await context_switcher(_handler(request))
    except HTTPMove as exc:
        if not intercept_redirects:
            raise
        # Intercept http redirect codes and display an html page with a
        # link to the target.
        if not getattr(exc, "location", None):
            raise
        response = web.Response(
            status=exc.status, reason=exc.reason, text=exc.text, headers=exc.headers
        )

        context = {"redirect_to": exc.location, "redirect_code": exc.status}

        _response = aiohttp_jinja2.render_template(
            "redirect.jinja2", request, context, app_key=TEMPLATE_KEY
        )
        response = _response
    except web.HTTPException:
        raise
    except Exception as e:
        if intercept_exc:
            tb = get_traceback(
                info=sys.exc_info(),
                skip=1,
                show_hidden_frames=False,
                ignore_system_exceptions=True,
                exc=e,
                app=request.app,
            )
            for frame in tb.frames:
                exc_history.frames[frame.id] = frame
            exc_history.tracebacks[tb.id] = tb
            request["pdbt_tb"] = tb

            # TODO: find out how to port following to aiohttp
            # or just remove it
            # token = request.app[APP_KEY]['pdtb_token']
            # qs = {'token': token, 'tb': str(tb.id)}
            # msg = 'Exception at %s\ntraceback url: %s'
            #
            # exc_url = request.app.router['debugtoolbar.exception']\
            #     .url(query=qs)
            # assert exc_url, msg
            # exc_msg = msg % (request.path, exc_url)
            # logger.exception(exc_msg)

            # subenviron = request.environ.copy()
            # del subenviron['PATH_INFO']
            # del subenviron['QUERY_STRING']
            # subrequest = type(request).blank(exc_url, subenviron)
            # subrequest.script_name = request.script_name
            # subrequest.path_info = \
            #     subrequest.path_info[len(request.script_name):]
            #
            # response = request.invoke_subrequest(subrequest)
            body = tb.render_full(request).encode("utf-8", "replace")
            response = web.Response(body=body, status=500, content_type="text/html")

            await toolbar.process_response(request, response)

            request["id"] = str(id(request))
            toolbar.status = response.status

            request_history.put(request["id"], toolbar)
            toolbar.inject(request, response)
            return response
        else:
            # logger.exception('Exception at %s' % request.path)
            raise e
    toolbar.status = response.status
    if intercept_redirects:
        # Intercept http redirect codes and display an html page with a
        # link to the target.
        if response.status in REDIRECT_CODES and getattr(response, "location", None):

            context = {
                "redirect_to": response.location,
                "redirect_code": response.status,
            }

            _response = aiohttp_jinja2.render_template(
                "redirect.jinja2", request, context, app_key=TEMPLATE_KEY
            )
            response = _response

    await toolbar.process_response(request, response)
    request["id"] = hexlify(id(request))

    # Don't store the favicon.ico request
    # it's requested by the browser automatically
    # Also ignore requests for debugtoolbar itself.
    tb_request = request.path.startswith(settings["path_prefix"])
    if not tb_request and request.path != "/favicon.ico":
        request_history.put(request["id"], toolbar)

    if not show_on_exc_only and response.content_type in HTML_TYPES:
        toolbar.inject(request, response)

    return response


toolbar_html_template = """\
<script type="text/javascript">
    var fileref=document.createElement("link")
    fileref.setAttribute("rel", "stylesheet")
    fileref.setAttribute("type", "text/css")
    fileref.setAttribute("href", "%(css_path)s")
    document.getElementsByTagName("head")[0].appendChild(fileref)
</script>

<div id="pDebug">
    <div style="display: block; %(button_style)s" id="pDebugToolbarHandle">
        <a title="Show Toolbar" id="pShowToolBarButton"
           href="%(toolbar_url)s" target="pDebugToolbar">&#171;
        FIXME: Debug Toolbar</a>
    </div>
</div>
"""
