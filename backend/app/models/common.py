"""Shared response envelopes for the API layer.

Every list endpoint returns ``ListResponse[T]`` so clients can rely on one
paging contract: ``count`` is the number of rows in this page, ``total`` the
number of rows matching the filters across all pages (null where the endpoint
paginates by cursor and a total would cost an extra scan without a consumer).
Pure pydantic, no FastAPI import, so the model layer stays framework-free.
"""

from pydantic import BaseModel


class ListResponse[ItemT](BaseModel):
    count: int
    total: int | None = None
    data: list[ItemT]
