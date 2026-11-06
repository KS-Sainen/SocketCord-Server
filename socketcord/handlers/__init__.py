"""API calls. Importing this package registers every route on `router`."""
from ..router import Router

router = Router()

from . import auth, chats, summary, users  # noqa: E402,F401  (registers the routes)
