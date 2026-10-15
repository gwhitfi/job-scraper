"""Robots rule selection with wildcard, end-anchor, and longest-match support."""

import math
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, str]] = field(default_factory=list)
    delay: float = 0


def normalized(path: str) -> str:
    path = quote(path, safe="/%:*?$&=;+,-._~!@()'")

    def decode(match):
        char = chr(int(match[0][1:], 16))
        return char if re.fullmatch(r"[a-zA-Z0-9._~-]", char) else match[0].upper()

    return re.sub(r"%[0-9a-fA-F]{2}", decode, path)


class Rules:
    def __init__(self, text: str, agent: str):
        groups: list[Group] = []
        current = Group()
        has_directives = False
        for line in text.lstrip("\ufeff").splitlines():
            key, sep, value = line.partition("#")[0].partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if has_directives:
                    groups.append(current)
                    current = Group()
                    has_directives = False
                current.agents.append(value.lower())
            elif current.agents:
                if key in ("allow", "disallow"):
                    has_directives = True
                    if value.startswith("/"):
                        current.rules.append((key, value))
                elif key == "crawl-delay":
                    has_directives = True
                    try:
                        delay = float(value)
                        if math.isfinite(delay) and delay >= 0:
                            current.delay = max(current.delay, delay)
                    except ValueError:
                        pass
        groups.append(current)
        token = agent.split("/")[0].lower()
        specific = [g for g in groups if token in g.agents]
        self.groups = specific or [g for g in groups if "*" in g.agents]
        self.delay = max([1.0, *(g.delay for g in self.groups)])

    def check(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        target = normalized((parts.path or "/") + ("?" + parts.query if parts.query else ""))
        matches = []
        for group in self.groups:
            for directive, raw in group.rules:
                pattern = normalized(raw)
                anchored = pattern.endswith("$")
                body = pattern[:-1] if anchored else pattern
                regex = "^" + ".*".join(re.escape(p) for p in body.split("*"))
                if anchored:
                    regex += "$"
                if re.search(regex, target):
                    matches.append(
                        (
                            len(body.replace("*", "").encode()),
                            directive == "allow",
                            f"{directive.title()}: {raw}",
                        )
                    )
        if not matches:
            return True, "No matching restriction"
        _, allowed, rule = max(matches)
        return allowed, rule
