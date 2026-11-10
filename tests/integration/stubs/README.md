Tiny stand-ins for fastapi, supabase, pydantic_settings and httpx, used only
when the real packages aren't installed (this folder is appended to the END of
sys.path, so real packages always win). They let the integration tests import
the routers and call the endpoint functions directly.
