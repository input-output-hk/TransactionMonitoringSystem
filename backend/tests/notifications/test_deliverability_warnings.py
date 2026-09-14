"""Write-time deliverability lint (config.deliverability_warnings).

Validation checks the document's SHAPE; these rules catch documents that pass
validation but silently deliver nothing (the send-time "config gap"). The
canonical case is the external review's misconfiguration probe: email enabled
with an empty recipient list and Critical routed to email only, accepted with
a bare 200. The rules mirror ``configWarnings`` in
``frontend/src/lib/notification-warnings.ts``; a rule added on either side
belongs on both.
"""

from app.notifications import config


def _doc(**overrides):
    """A fully deliverable baseline: email on with a recipient, routed."""
    doc = {
        "version": 1,
        "channels": {"email": {"enabled": True, "recipients": ["ops@x.com"]}},
        "groups": {},
        "triggers": {"defaults": {"Critical": ["email"]}, "rules": []},
    }
    doc.update(overrides)
    return doc


def test_deliverable_doc_has_no_warnings():
    assert config.deliverability_warnings(_doc()) == []


def test_audit_probe_email_routed_with_empty_recipients():
    # The exact document the external review stored: accepted, delivers nothing.
    doc = _doc(channels={"email": {"enabled": True, "recipients": []}})
    warnings = config.deliverability_warnings(doc)
    assert len(warnings) == 1
    assert "no recipients" in warnings[0]


def test_routed_but_disabled_channel_warns():
    doc = _doc(channels={"email": {"enabled": False, "recipients": ["ops@x.com"]}})
    warnings = config.deliverability_warnings(doc)
    assert len(warnings) == 1
    assert "disabled" in warnings[0]


def test_webhook_routed_in_defaults_without_url_warns():
    doc = _doc(
        channels={
            "email": {"enabled": True, "recipients": ["ops@x.com"]},
            "webhook": {"enabled": True, "default_url": ""},
        },
        triggers={"defaults": {"Critical": ["email", "webhook"]}, "rules": []},
    )
    warnings = config.deliverability_warnings(doc)
    assert any("no default_url" in w for w in warnings)


def test_rule_routes_webhook_without_any_url_warns():
    doc = _doc(
        channels={
            "email": {"enabled": True, "recipients": ["ops@x.com"]},
            "webhook": {"enabled": True, "default_url": ""},
        },
        triggers={
            "defaults": {"Critical": ["email"]},
            "rules": [{"band": "High", "attack_classes": ["phishing"], "channels": ["webhook"]}],
        },
    )
    warnings = config.deliverability_warnings(doc)
    assert any("per-class rule routes to webhook" in w for w in warnings)


def test_rule_with_own_webhook_url_does_not_warn():
    doc = _doc(
        channels={
            "email": {"enabled": True, "recipients": ["ops@x.com"]},
            "webhook": {"enabled": True, "default_url": ""},
        },
        triggers={
            "defaults": {"Critical": ["email"]},
            "rules": [
                {
                    "band": "High",
                    "attack_classes": ["phishing"],
                    "channels": ["webhook"],
                    "webhook_url": "https://hooks.example.com/x",
                }
            ],
        },
    )
    assert config.deliverability_warnings(doc) == []


def test_group_alias_expanding_to_zero_counts_as_no_recipients():
    # A recipient list of one alias whose group is empty resolves to zero
    # addresses, matching resolve_recipients (counting the raw list would read
    # ["group:soc-team"] as covered while dispatch delivers nothing).
    doc = _doc(
        channels={"email": {"enabled": True, "recipients": ["group:soc-team"]}},
        groups={"soc-team": []},
    )
    warnings = config.deliverability_warnings(doc)
    assert len(warnings) == 1
    assert "no recipients" in warnings[0]


def test_rule_override_with_empty_list_warns_despite_populated_default():
    # A per-rule override REPLACES the channel default (key presence decides),
    # so an empty override delivers nothing even though the default has an
    # address.
    doc = _doc(
        triggers={
            "defaults": {"Critical": ["email"]},
            "rules": [
                {
                    "band": "High",
                    "attack_classes": ["phishing"],
                    "channels": ["email"],
                    "recipients": {"email": []},
                }
            ],
        },
    )
    warnings = config.deliverability_warnings(doc)
    assert any("replaces the channel default" in w for w in warnings)


def test_rule_without_override_falls_back_to_populated_default():
    doc = _doc(
        triggers={
            "defaults": {"Critical": ["email"]},
            "rules": [{"band": "High", "attack_classes": ["phishing"], "channels": ["email"]}],
        },
    )
    assert config.deliverability_warnings(doc) == []


def test_enabled_but_unrouted_channel_warns():
    doc = _doc(triggers={"defaults": {}, "rules": []})
    warnings = config.deliverability_warnings(doc)
    assert len(warnings) == 1
    assert "never fires" in warnings[0]
