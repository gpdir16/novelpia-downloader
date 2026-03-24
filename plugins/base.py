from dataclasses import dataclass


@dataclass
class Episode:
    order: int
    episode_id: int
    title: str


@dataclass
class RequestSpec:
    method: str
    url: str
    data: dict | None = None
    referer: str | None = None
    accept: str = "*/*"


@dataclass
class EpisodeContent:
    body: str
