"""Validated, secret-free settings shared by the native app and MCP."""
from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Provider(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    name: str = Field(min_length=1, max_length=200)
    protocol: Literal["openai", "anthropic", "jev"] = "openai"
    baseUrl: str
    enabled: bool = True
    manualModels: list[str] = Field(default_factory=list)
    allowLocal: bool = False

    @model_validator(mode="after")
    def check_url(self):
        url = urlsplit(self.baseUrl)
        if not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Endpoint must not contain credentials, query or fragment")
        try:
            is_loopback = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            is_loopback = url.hostname == "localhost"
        if url.scheme != "https" and not (url.scheme == "http" and is_loopback and self.allowLocal):
            raise ValueError("HTTPS required; loopback HTTP needs explicit allowLocal")
        self.baseUrl = self.baseUrl.rstrip("/")
        return self


class Route(Model):
    providerId: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    model: str = Field(min_length=1, max_length=300)
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None


class Member(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    label: str = Field(min_length=1, max_length=200)
    routes: list[Route] = Field(min_length=1)


class Limits(Model):
    maxTokens: int | None = Field(default=None, ge=1)
    timeSeconds: float | None = Field(default=None, gt=0)
    budgetUsd: float | None = Field(default=None, ge=0)


class Agent(Model):
    model: str | None = None
    effort: str | None = "high"


class Pair(Model):
    codex: Agent = Field(default_factory=lambda: Agent(model="gpt-6-sol"))
    claude: Agent = Field(default_factory=lambda: Agent(model="claude-opus-5-5"))
    projectRoots: list[str] = Field(default_factory=list)

    @field_validator("projectRoots")
    @classmethod
    def validate_roots(cls, values):
        result = []
        for value in values:
            path = Path(value).expanduser().resolve(strict=True)
            if not path.is_dir() or path == Path("/") or path == Path.home():
                raise ValueError("Choose a specific existing project directory")
            result.append(str(path))
        return list(dict.fromkeys(result))


class Jev(Model):
    enabled: bool = False
    providerId: str | None = None
    model: str = "jev-latest"


class Config(Model):
    version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    providers: list[Provider] = Field(default_factory=list)
    members: list[Member] = Field(default_factory=list)
    synthesis: Route | None = None
    pair: Pair = Field(default_factory=Pair)
    limits: Limits = Field(default_factory=Limits)
    jev: Jev = Field(default_factory=Jev)

    @model_validator(mode="after")
    def references(self):
        provider_ids = [p.id for p in self.providers]
        if {"codex", "claude"} & set(provider_ids):
            raise ValueError("Codex and Claude IDs are reserved for official agents")
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("Duplicate provider IDs")
        member_ids = [m.id for m in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("Duplicate member IDs")
        allowed = set(provider_ids) | {"codex", "claude"}
        routes = [r for m in self.members for r in m.routes]
        if self.synthesis:
            routes.append(self.synthesis)
        if any(r.providerId not in allowed for r in routes):
            raise ValueError("Route references an unknown provider")
        if self.jev.providerId and self.jev.providerId not in provider_ids:
            raise ValueError("Jev references an unknown provider")
        return self


def validate_config(value: dict) -> dict:
    return Config.model_validate(value).model_dump(mode="json")


def defaults() -> dict:
    return Config().model_dump(mode="json")
