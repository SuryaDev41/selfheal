"""Generated site profiles block ad requests before browser tests start."""

from __future__ import annotations

import yaml

from selfheal.config import load_app_config
from selfheal.onboarding import _save_site_profile, prepare_site, site_name
from selfheal.site_policy import install_blocked_resource_routes


def test_automationexercise_profile_blocks_vignette_hosts(tmp_path):
    profile = tmp_path / "site.yaml"
    profile.write_text(yaml.safe_dump({
        "environments": {"prod": {"base_url": "https://www.automationexercise.com"}},
        "execution": {"blocked_resource_host_suffixes": ["example.net"]},
    }), encoding="utf-8")

    config = load_app_config(profile)

    assert config["execution"]["blocked_resource_host_suffixes"] == [
        "example.net", "googlesyndication.com", "doubleclick.net",
    ]


def test_other_sites_keep_their_explicit_resource_policy(tmp_path):
    profile = tmp_path / "site.yaml"
    profile.write_text(yaml.safe_dump({
        "environments": {"prod": {"base_url": "https://example.com"}},
        "execution": {"blocked_resource_host_suffixes": ["example.net"]},
    }), encoding="utf-8")

    assert load_app_config(profile)["execution"]["blocked_resource_host_suffixes"] == ["example.net"]


def test_ad_route_is_registered_before_navigation():
    routes = []

    class Context:
        def route(self, pattern, handler):
            routes.append((pattern, handler))

    class Route:
        aborted = False

        def abort(self):
            self.aborted = True

    install_blocked_resource_routes(Context(), ["googlesyndication.com"])

    pattern, handler = routes[0]
    assert pattern.search("https://pagead2.googlesyndication.com/pagead/ads")
    assert not pattern.search("https://www.automationexercise.com/products")
    route = Route()
    handler(route)
    assert route.aborted


def test_generated_profile_keeps_ad_policy_and_execution_overrides(tmp_path):
    site = "https://www.automationexercise.com"
    tests = [{
        "id": "TC_001", "title": "Home", "step": "OPEN | /\nASSERT_VISIBLE | css=header",
        "expected": "", "data": "",
    }]
    prepared = prepare_site(site, tests, root=tmp_path)
    profile = prepared.profile_path
    config = yaml.safe_load(profile.read_text(encoding="utf-8"))
    assert prepared.discovery_calls == 0
    assert config["execution"]["blocked_resource_host_suffixes"] == [
        "googlesyndication.com", "doubleclick.net",
    ]

    config["execution"]["delay_between_cases_seconds"] = 2
    profile.write_text(yaml.safe_dump(config), encoding="utf-8")
    _save_site_profile(tmp_path, site, site_name(site), "fixture", {}, {}, 0, 0, "new")
    refreshed = yaml.safe_load(profile.read_text(encoding="utf-8"))
    assert refreshed["execution"]["delay_between_cases_seconds"] == 2
    assert refreshed["execution"]["blocked_resource_host_suffixes"] == [
        "googlesyndication.com", "doubleclick.net",
    ]
