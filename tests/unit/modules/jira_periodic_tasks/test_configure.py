"""Tests for the JiraPeriodicConfigurer service (DB-free, injected fake ToolboxClient)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

from agento.modules.jira.src.toolbox_client import ToolboxAPIError
from agento.modules.jira_periodic_tasks.src.configure import (
    JiraPeriodicConfigurer,
    _jql_escape,
    _valid_project_key,
    render_report,
)

FIELD_ID = "customfield_10709"
ADMIN = {"auth_user": "admin@example.com", "auth_token": "secret"}
LOGGER = logging.getLogger("test-configure")


def _make_toolbox(responses=None, search=None):
    """Fake ToolboxClient: keyed jira_request side_effect + jira_search return."""
    toolbox = MagicMock()
    default = {
        ("GET", "/rest/api/3/myself"): {"accountId": "1"},
        ("GET", "/rest/api/3/project/AI"): {"id": "10001", "style": "classic"},
        ("GET", "/rest/api/3/project/AI/statuses"): [
            {"statuses": [{"name": "Periodic", "id": "100"}]}
        ],
        ("GET", "/rest/api/3/field"): [
            {"name": "Frequency", "id": FIELD_ID, "custom": True,
             "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}}
        ],
        ("GET", f"/rest/api/3/field/{FIELD_ID}/context"): {
            "values": [{"id": "ctx1", "isGlobalContext": True}]
        },
        ("GET", f"/rest/api/3/field/{FIELD_ID}/context/ctx1/option"): {
            "values": [{"value": "Every 5min"}, {"value": "Daily at 8:00"}]
        },
        ("POST", f"/rest/api/3/field/{FIELD_ID}/context/ctx1/option"): {},
        ("GET", "/rest/api/3/screens"): {"values": [{"id": "1", "name": "Default"}]},
    }
    if responses:
        default.update(responses)

    def side_effect(method, path, body=None, **kwargs):
        key = (method, path)
        if key in default:
            v = default[key]
            if isinstance(v, Exception):
                raise v
            return v
        if "/tabs" in path and "/fields" not in path:
            return [{"id": "tab1"}]
        return {}

    toolbox.jira_request.side_effect = side_effect
    toolbox.jira_search.return_value = search if search is not None else {"issues": []}
    return toolbox


def _mutating_calls(toolbox):
    return [
        c for c in toolbox.jira_request.call_args_list
        if c.args and c.args[0] in ("POST", "PUT", "DELETE")
    ]


def _freq_map():
    return {"Every 5min": "*/5 * * * *", "Daily at 8:00": "0 8 * * *"}


def _service(toolbox, *, projects=("AI",), status_name="Periodic", field_id=FIELD_ID,
             frequency_map=None, admin_auth=ADMIN, check=False):
    return JiraPeriodicConfigurer(
        toolbox,
        projects=list(projects),
        status_name=status_name,
        field_id=field_id,
        frequency_map=_freq_map() if frequency_map is None else frequency_map,
        admin_auth=admin_auth,
        logger=LOGGER,
        check=check,
    )


# --- unit helpers ---------------------------------------------------------------

def test_valid_project_key():
    assert _valid_project_key("AI")
    assert _valid_project_key("PROJ_1")
    assert not _valid_project_key("AI; DROP")
    assert not _valid_project_key('AI"')
    assert not _valid_project_key("1AI")
    assert not _valid_project_key("")


def test_jql_escape():
    assert _jql_escape('Peri"odic') == 'Peri\\"odic'
    assert _jql_escape("back\\slash") == "back\\\\slash"


# --- apply ----------------------------------------------------------------------

def test_apply_adds_missing_option():
    # existing has only "Every 5min"; frequency_map wants "Daily at 8:00" too.
    tb = _make_toolbox({
        ("GET", f"/rest/api/3/field/{FIELD_ID}/context/ctx1/option"): {"values": [{"value": "Every 5min"}]},
    })
    report = _service(tb).run()
    assert report.resolved_field_id == FIELD_ID
    assert report.options_added == ["Daily at 8:00"]
    assert report.failed is False
    posts = [c for c in _mutating_calls(tb) if c.args[1].endswith("/option")]
    assert len(posts) == 1


def test_apply_idempotent_no_mutation_when_in_sync():
    tb = _make_toolbox()  # existing already has both keys
    report = _service(tb).run()
    assert report.options_added == []
    assert not any(c.args[1].endswith("/option") for c in _mutating_calls(tb))


def test_apply_creates_missing_status_per_project():
    tb = _make_toolbox({
        ("GET", "/rest/api/3/project/AI/statuses"): [{"statuses": []}],  # missing
        ("GET", "/rest/api/3/project/AI"): {"id": "10001", "style": "classic"},
        ("POST", "/rest/api/3/statuses"): [{"id": "555"}],
    })
    report = _service(tb).run()
    pr = report.projects[0]
    assert pr.status_created is True
    assert pr.failed is False


def test_field_resolved_once_across_projects():
    tb = _make_toolbox({
        ("GET", "/rest/api/3/project/BUG"): {"id": "20002", "style": "classic"},
        ("GET", "/rest/api/3/project/BUG/statuses"): [{"statuses": [{"name": "Periodic", "id": "9"}]}],
    })
    _service(tb, projects=["AI", "BUG"]).run()
    field_lookups = [
        c for c in tb.jira_request.call_args_list
        if c.args[:2] == ("GET", "/rest/api/3/field")
    ]
    assert len(field_lookups) == 1


def test_options_synced_once_for_multiple_projects():
    tb = _make_toolbox({
        ("GET", f"/rest/api/3/field/{FIELD_ID}/context/ctx1/option"): {"values": [{"value": "Every 5min"}]},
        ("GET", "/rest/api/3/project/BUG"): {"id": "20002", "style": "classic"},
        ("GET", "/rest/api/3/project/BUG/statuses"): [{"statuses": [{"name": "Periodic", "id": "9"}]}],
    })
    _service(tb, projects=["AI", "BUG"]).run()
    posts = [c for c in _mutating_calls(tb) if c.args[1].endswith("/option")]
    assert len(posts) == 1


def test_apply_requires_admin_to_create_missing_field():
    tb = _make_toolbox({("GET", "/rest/api/3/field"): []})  # field missing
    report = _service(tb, field_id=None, admin_auth=None).run()  # no stored id → no id-fallback
    assert report.resolved_field_id is None
    assert report.failed is True
    assert not any(c.args[:2] == ("POST", "/rest/api/3/field") for c in tb.jira_request.call_args_list)


def test_empty_frequency_map_no_option_calls():
    tb = _make_toolbox()
    report = _service(tb, frequency_map={}).run()
    assert report.options_checked is False
    assert not any("/option" in c.args[1] for c in tb.jira_request.call_args_list)


def test_next_gen_apply_associates_field_and_guides():
    # C12: team-managed apply auto-associates the global field with the project (best-effort),
    # then guides the operator to add it to the work type. Screen mapping is NOT used.
    tb = _make_toolbox({("GET", "/rest/api/3/project/AI"): {"id": "10001", "style": "next-gen"}})
    report = _service(tb).run()  # apply
    pr = report.projects[0]
    assert pr.team_managed is True
    assert pr.field_associated is True
    assert not any(c.args[1] == "/rest/api/3/screens" for c in tb.jira_request.call_args_list)
    assoc = [c for c in tb.jira_request.call_args_list if c.args[:2] == ("PUT", "/rest/api/3/field/association")]
    assert len(assoc) == 1
    body = assoc[0].args[2]
    assert body["associationContexts"][0] == {"type": "PROJECT_ID", "identifier": 10001}
    assert body["fields"][0] == {"type": "FIELD_ID", "identifier": FIELD_ID}
    out = render_report(report, check=False)
    assert "team-managed" in out and "now available" in out and "Project settings" in out


def test_next_gen_check_does_not_associate():
    tb = _make_toolbox({("GET", "/rest/api/3/project/AI"): {"id": "10001", "style": "next-gen"}})
    report = _service(tb, check=True).run()  # read-only
    assert report.projects[0].team_managed is True
    assert report.projects[0].field_associated is False
    assert not any(c.args[:2] == ("PUT", "/rest/api/3/field/association") for c in tb.jira_request.call_args_list)
    out = render_report(report, check=True)  # guidance without association
    assert "team-managed" in out and "add the" in out and "Project settings" in out


def test_company_managed_project_no_association_no_team_guidance():
    tb = _make_toolbox()  # AI is style "classic"
    report = _service(tb).run()
    assert report.projects[0].team_managed is False
    assert not any(c.args[:2] == ("PUT", "/rest/api/3/field/association") for c in tb.jira_request.call_args_list)
    assert "team-managed" not in render_report(report, check=False)


def test_duplicate_projects_deduped():
    tb = _make_toolbox()
    report = _service(tb, projects=["AI", "AI"]).run()
    assert len(report.projects) == 1


def test_field_id_drift_flagged():
    tb = _make_toolbox()
    report = _service(tb, field_id="customfield_OLD").run()
    assert report.resolved_field_id == FIELD_ID
    assert report.field_drift is True


def test_id_fallback_reuses_stored_field_when_name_lookup_misses():
    # Jira field list doesn't reflect the field (name lookup empty), but the stored id still
    # exists (its context is readable) -> reuse it instead of creating a duplicate (C9).
    tb = _make_toolbox({("GET", "/rest/api/3/field"): []})  # name lookup finds nothing
    report = _service(tb, field_id=FIELD_ID).run()  # apply
    assert report.resolved_field_id == FIELD_ID
    assert report.field_created is False
    assert not any(c.args[:2] == ("POST", "/rest/api/3/field") for c in tb.jira_request.call_args_list)


def test_id_fallback_creates_when_stored_id_also_gone():
    tb = _make_toolbox({
        ("GET", "/rest/api/3/field"): [],
        ("GET", "/rest/api/3/field/customfield_GONE/context"): ToolboxAPIError(404, "gone"),
        ("POST", "/rest/api/3/field"): {"id": "customfield_NEW", "name": "Frequency"},
        ("GET", "/rest/api/3/field/customfield_NEW/context"): {"values": [{"id": "ctx1", "isGlobalContext": True}]},
        ("GET", "/rest/api/3/field/customfield_NEW/context/ctx1/option"): {"values": []},
        ("POST", "/rest/api/3/field/customfield_NEW/context/ctx1/option"): {},
    })
    report = _service(tb, field_id="customfield_GONE").run()  # apply
    assert report.resolved_field_id == "customfield_NEW"
    assert report.field_created is True


# --- check ----------------------------------------------------------------------

def test_check_consistent_makes_zero_mutations():
    tb = _make_toolbox()
    report = _service(tb, check=True).run()
    assert report.inconsistent is False
    assert report.incomplete is False
    assert _mutating_calls(tb) == []


def test_check_flags_field_missing_and_skips_scan():
    tb = _make_toolbox({("GET", "/rest/api/3/field"): []})
    report = _service(tb, field_id=None, check=True).run()  # no stored id → genuinely missing
    assert report.resolved_field_id is None
    assert report.inconsistent is True
    tb.jira_search.assert_not_called()  # short-circuit: no fields=[None] scan


def test_check_flags_status_missing():
    tb = _make_toolbox({("GET", "/rest/api/3/project/AI/statuses"): [{"statuses": []}]})
    report = _service(tb, check=True).run()
    assert report.projects[0].status_ok is False
    assert report.projects[0].inconsistent is True
    assert report.inconsistent is True


def test_check_flags_options_missing():
    tb = _make_toolbox({
        ("GET", f"/rest/api/3/field/{FIELD_ID}/context/ctx1/option"): {"values": [{"value": "Every 5min"}]},
    })
    report = _service(tb, check=True).run()
    assert report.missing_options == ["Daily at 8:00"]
    assert report.inconsistent is True
    assert _mutating_calls(tb) == []


def test_check_flags_extra_options_two_way():
    # A Jira option absent from frequency_map breaks the mapping both ways (an issue set to
    # it is silently skipped by sync), so --check must flag it inconsistent.
    tb = _make_toolbox({
        ("GET", f"/rest/api/3/field/{FIELD_ID}/context/ctx1/option"): {
            "values": [{"value": "Every 5min"}, {"value": "Daily at 8:00"}, {"value": "Legacy option"}]
        },
    })
    report = _service(tb, check=True).run()
    assert report.missing_options == []
    assert report.extra_options == ["Legacy option"]
    assert report.inconsistent is True


def test_check_flags_field_id_drift_inconsistent():
    # Stored id is stale → sync reads the wrong field and silently skips everything.
    tb = _make_toolbox()
    report = _service(tb, field_id="customfield_OLD", check=True).run()
    assert report.field_drift is True
    assert report.resolved_field_id == FIELD_ID
    assert report.inconsistent is True


def test_check_scan_failure_is_incomplete():
    tb = _make_toolbox()
    tb.jira_search.side_effect = ToolboxAPIError(500, "boom")
    report = _service(tb, check=True).run()
    assert report.incomplete is True
    assert report.projects[0].incomplete is True


def test_check_truncated_scan_is_incomplete():
    # exactly 100 issues → only the first page seen; unresolvable issues may exist beyond.
    tb = _make_toolbox(search={"issues": [{"key": f"AI-{i}", "fields": {FIELD_ID: {"value": "Every 5min"}}}
                                          for i in range(100)]})
    report = _service(tb, check=True).run()
    assert report.projects[0].truncated is True
    assert report.incomplete is True


def test_check_flags_unresolvable_issues_from_fixture():
    fixture = json.loads(
        (Path(__file__).parents[3] / "fixtures" / "jira_search_cykliczne.json").read_text()
    )
    # frequency_map maps the two "good" labels; AI-4 (null) + AI-5 ("Co 2 lata") are unresolvable.
    fmap = {"Co 5min": "*/5 * * * *", "1x dziennie o 8:00": "0 8 * * *"}
    tb = _make_toolbox(
        {("GET", f"/rest/api/3/field/{FIELD_ID}/context/ctx1/option"): {
            "values": [{"value": "Co 5min"}, {"value": "1x dziennie o 8:00"}]}},
        search=fixture,
    )
    report = _service(tb, check=True, frequency_map=fmap).run()
    pr = report.projects[0]
    reasons = dict(pr.unresolvable)
    assert reasons.get("AI-4") == "no value"
    assert reasons.get("AI-5") == "unknown"
    assert report.inconsistent is True


def test_check_without_admin_is_incomplete_not_clean():
    tb = _make_toolbox()
    report = _service(tb, check=True, admin_auth=None).run()
    assert report.incomplete is True
    assert report.options_checked is False
    # never touched the admin-only context/option endpoints
    assert not any("/context" in c.args[1] for c in tb.jira_request.call_args_list)


def test_malformed_project_key_rejected_before_request():
    tb = _make_toolbox()
    bad = 'AI"; evil'
    report = _service(tb, projects=[bad], check=True).run()
    pr = report.projects[0]
    assert pr.error == "invalid project key"
    assert pr.inconsistent is True
    assert not any(bad in c.args[1] for c in tb.jira_request.call_args_list)


def test_jql_status_value_escaped():
    tb = _make_toolbox()
    _service(tb, status_name='Peri"odic', check=True).run()
    tb.jira_search.assert_called_once()
    jql = tb.jira_search.call_args.kwargs.get("jql") or tb.jira_search.call_args.args[0]
    assert 'status = "Peri\\"odic"' in jql


def test_project_404_continues_to_next():
    tb = _make_toolbox({
        ("GET", "/rest/api/3/project/BUG"): ToolboxAPIError(404, "not found"),
        ("GET", "/rest/api/3/project/AI"): {"id": "10001", "style": "classic"},
    })
    report = _service(tb, projects=["BUG", "AI"], check=True).run()
    assert len(report.projects) == 2
    bug = next(p for p in report.projects if p.key == "BUG")
    ai = next(p for p in report.projects if p.key == "AI")
    assert bug.error == "project 404" and bug.inconsistent is True
    assert ai.status_ok is True


def test_render_report_shows_options_not_verified_when_no_admin():
    tb = _make_toolbox()
    report = _service(tb, check=True, admin_auth=None).run()
    assert report.options_need_admin is True
    out = render_report(report, check=True)
    assert "not verified" in out and "no admin token" in out
    assert "exits non-zero" in out  # check surfaces the incompleteness in the result line


def test_scan_incomplete_with_empty_map_is_not_options_unknown():
    # frequency_map empty (options intentionally skipped) + a failed issue scan → the report must
    # attribute incompleteness to the SCAN, not to options (R9: options_incomplete is separate).
    tb = _make_toolbox()
    tb.jira_search.side_effect = ToolboxAPIError(500, "boom")
    report = _service(tb, check=True, frequency_map={}).run()
    assert report.options_incomplete is False
    assert report.incomplete is True  # from the failed scan, not options
    out = render_report(report, check=True)
    assert "Dropdown options" not in out  # options weren't the problem
    assert "issue scan incomplete" in out


def test_check_result_names_missing_field():
    # A global failure (no Frequency field) must appear in the result line, not leave it blank.
    tb = _make_toolbox({("GET", "/rest/api/3/field"): []})
    report = _service(tb, field_id=None, check=True).run()
    out = render_report(report, check=True)
    assert "Frequency field missing" in out
    assert "(command exits non-zero)" in out


def test_check_result_names_options_out_of_sync():
    tb = _make_toolbox({
        ("GET", f"/rest/api/3/field/{FIELD_ID}/context/ctx1/option"): {"values": [{"value": "Every 5min"}]},
    })
    report = _service(tb, check=True).run()  # 'Daily at 8:00' missing
    out = render_report(report, check=True)
    assert "dropdown options out of sync" in out
    assert "(command exits non-zero)" in out


def test_check_result_singular_grammar():
    tb = _make_toolbox({("GET", "/rest/api/3/project/AI/statuses"): [{"statuses": []}]})  # status missing
    report = _service(tb, check=True).run()
    out = render_report(report, check=True)
    assert "1 project needs attention" in out  # singular grammar


def test_options_unreadable_context_reports_not_admin_reason():
    # Admin present but the field's context can't be read (e.g. a team-managed field with no
    # global context) → incomplete, but the message must NOT blame the admin token.
    tb = _make_toolbox({("GET", f"/rest/api/3/field/{FIELD_ID}/context"): ToolboxAPIError(404, "no context")})
    report = _service(tb, check=True).run()
    assert report.incomplete is True
    assert report.options_need_admin is False
    out = render_report(report, check=True)
    assert "not verified" in out and "could not be read" in out
    assert "no admin token" not in out  # admin is present; don't blame the token
