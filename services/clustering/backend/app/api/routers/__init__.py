"""APIRouter modules, one per resource group. ``main`` assembles them under
``/api/v1`` (canonical, in the OpenAPI schema) and ``/api`` (legacy alias for
the bundled UI). All routers except ``system`` require the API key."""
