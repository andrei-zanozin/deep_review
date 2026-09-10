from __future__ import annotations

import pytest

from deep_review.discovery import _validate_people
from deep_review.errors import WorkflowError
from deep_review.models import DiscoveryResult


def test_reviewer_mismatch_identifies_discovery_and_jira_users() -> None:
    discovery = DiscoveryResult(
        reviewer={"username": "discovered-user", "display_name": "Discovered User"},
        requestor={"username": "requestor"},
        review_type="primary",
    )
    issue = {
        "assignee": {
            "name": "jira-user",
            "displayName": "Jira User",
        }
    }

    with pytest.raises(WorkflowError) as error:
        _validate_people(issue, [], discovery)

    assert str(error.value) == (
        "reviewer (username='discovered-user', display_name='Discovered User') "
        "does not match Jira assignee (username='jira-user', display_name='Jira User')"
    )
