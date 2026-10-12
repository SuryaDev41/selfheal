"""Site-specific browser resource policies for generated test profiles."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

_AUTOMATION_EXERCISE_AD_HOSTS = ("googlesyndication.com", "doubleclick.net")


def blocked_resource_hosts(base_url: str, configured: Iterable[str] = ()) -> list[str]:
    """Retain configured blocks and disable vignette ads on AutomationExercise."""
    hosts = [str(host).strip().casefold() for host in configured if str(host).strip()]
    site_host = (urlsplit(base_url).hostname or "").casefold()
    if site_host == "automationexercise.com" or site_host.endswith(".automationexercise.com"):
        hosts.extend(_AUTOMATION_EXERCISE_AD_HOSTS)
    return list(dict.fromkeys(hosts))


def install_blocked_resource_routes(context, hosts: Iterable[str]) -> None:
    """Abort configured third-party requests before a page is opened."""
    for host in hosts:
        context.route(
            re.compile(rf"^https?://(?:[^/]+\.)?{re.escape(host)}(?::\d+)?/", re.I),
            lambda route: route.abort(),
        )
