"""Permission catalogue and built-in role definitions.

RBAC model
----------
A ``Permission`` is a ``resource:action`` string. Roles map to sets of
permissions. Workspace memberships carry exactly one built-in role (or a custom
role, which is just a named permission set persisted in ``roles``).

Adding a new capability = add a Permission member + attach it to the roles that
should have it + use ``require(Permission.X)`` in the route. Nothing else needs
to change, which keeps the model extensible without touching enforcement code.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Permission(StrEnum):
    # workspace
    WORKSPACE_READ = "workspace:read"
    WORKSPACE_UPDATE = "workspace:update"
    WORKSPACE_DELETE = "workspace:delete"
    WORKSPACE_TRANSFER = "workspace:transfer"

    # members / roles
    MEMBER_INVITE = "member:invite"
    MEMBER_REMOVE = "member:remove"
    MEMBER_ROLE_UPDATE = "member:role:update"
    ROLE_MANAGE = "role:manage"

    # social accounts
    SOCIAL_ACCOUNT_READ = "social_account:read"
    SOCIAL_ACCOUNT_CONNECT = "social_account:connect"
    SOCIAL_ACCOUNT_DISCONNECT = "social_account:disconnect"

    # content
    CONTENT_READ = "content:read"
    CONTENT_CREATE = "content:create"
    CONTENT_UPDATE = "content:update"
    CONTENT_DELETE = "content:delete"
    CONTENT_SUBMIT = "content:submit"
    CONTENT_APPROVE = "content:approve"
    CONTENT_PUBLISH = "content:publish"
    CONTENT_CANCEL = "content:cancel"

    # media
    MEDIA_READ = "media:read"
    MEDIA_UPLOAD = "media:upload"
    MEDIA_DELETE = "media:delete"

    # AI
    AI_GENERATE = "ai:generate"
    AI_BUDGET_MANAGE = "ai:budget:manage"

    # inbox / comments
    INBOX_READ = "inbox:read"
    INBOX_REPLY = "inbox:reply"
    INBOX_ASSIGN = "inbox:assign"
    INBOX_AUTOMATION_MANAGE = "inbox:automation:manage"
    COMMENTS_READ = "comments:read"
    COMMENTS_REPLY = "comments:reply"
    COMMENTS_HIDE = "comments:hide"

    # CRM
    CRM_READ = "crm:read"
    CRM_WRITE = "crm:write"
    CRM_DELETE = "crm:delete"
    CRM_EXPORT = "crm:export"

    # analytics
    ANALYTICS_READ = "analytics:read"
    ANALYTICS_EXPORT = "analytics:export"

    # billing
    BILLING_READ = "billing:read"
    BILLING_MANAGE = "billing:manage"

    # audit / settings
    AUDIT_READ = "audit:read"
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"

    # agents
    AGENT_RUN = "agent:run"
    AGENT_APPROVE_TOOL = "agent:tool:approve"


class Role(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    EDITOR = "EDITOR"
    SOCIAL_MEDIA_MANAGER = "SOCIAL_MEDIA_MANAGER"
    ANALYST = "ANALYST"
    SUPPORT = "SUPPORT"
    VIEWER = "VIEWER"


_ALL = frozenset(Permission)

_PUBLISHING = (
    Permission.CONTENT_PUBLISH,
    Permission.CONTENT_CANCEL,
)

_CONTENT_AUTHORING = (
    Permission.CONTENT_READ,
    Permission.CONTENT_CREATE,
    Permission.CONTENT_UPDATE,
    Permission.CONTENT_SUBMIT,
    Permission.MEDIA_READ,
    Permission.MEDIA_UPLOAD,
    Permission.AI_GENERATE,
)

_ENGAGEMENT = (
    Permission.INBOX_READ,
    Permission.INBOX_REPLY,
    Permission.INBOX_ASSIGN,
    Permission.COMMENTS_READ,
    Permission.COMMENTS_REPLY,
)

_READ_ONLY = (
    Permission.WORKSPACE_READ,
    Permission.SOCIAL_ACCOUNT_READ,
    Permission.CONTENT_READ,
    Permission.MEDIA_READ,
    Permission.INBOX_READ,
    Permission.COMMENTS_READ,
    Permission.ANALYTICS_READ,
    Permission.CRM_READ,
    Permission.BILLING_READ,
)

BUILTIN_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: _ALL,
    Role.ADMIN: _ALL
    - {
        Permission.WORKSPACE_DELETE,
        Permission.WORKSPACE_TRANSFER,
    },
    Role.EDITOR: frozenset(
        _CONTENT_AUTHORING
        + _ENGAGEMENT
        + _PUBLISHING
        + (
            Permission.WORKSPACE_READ,
            Permission.SOCIAL_ACCOUNT_READ,
            Permission.CONTENT_APPROVE,
            Permission.CONTENT_DELETE,
            Permission.MEDIA_DELETE,
            Permission.CRM_READ,
            Permission.CRM_WRITE,
            Permission.ANALYTICS_READ,
            Permission.AGENT_RUN,
        )
    ),
    Role.SOCIAL_MEDIA_MANAGER: frozenset(
        _CONTENT_AUTHORING
        + _ENGAGEMENT
        + _PUBLISHING
        + (
            Permission.WORKSPACE_READ,
            Permission.SOCIAL_ACCOUNT_READ,
            Permission.INBOX_AUTOMATION_MANAGE,
            Permission.COMMENTS_HIDE,
            Permission.CRM_READ,
            Permission.CRM_WRITE,
            Permission.ANALYTICS_READ,
            Permission.AGENT_RUN,
            Permission.SETTINGS_READ,
        )
    ),
    Role.ANALYST: frozenset(
        _READ_ONLY + (Permission.ANALYTICS_EXPORT, Permission.CRM_EXPORT, Permission.AGENT_RUN)
    ),
    Role.SUPPORT: frozenset(
        _ENGAGEMENT
        + (
            Permission.WORKSPACE_READ,
            Permission.SOCIAL_ACCOUNT_READ,
            Permission.CONTENT_READ,
            Permission.CRM_READ,
            Permission.CRM_WRITE,
        )
    ),
    Role.VIEWER: frozenset(_READ_ONLY),
}


def permissions_for_roles(roles: Iterable[str]) -> frozenset[Permission]:
    """Resolve a membership's effective permission set."""
    resolved: set[Permission] = set()
    for role in roles:
        try:
            resolved |= set(BUILTIN_ROLE_PERMISSIONS[Role(role)])
        except (KeyError, ValueError):
            # Custom roles are resolved from the database at request time; an
            # unknown string here simply contributes nothing.
            continue
    return frozenset(resolved)


def role_has_permission(role: Role | str, permission: Permission) -> bool:
    try:
        return permission in BUILTIN_ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Sensitive agent tools — always require an explicit human-approved permission.
# --------------------------------------------------------------------------- #
SENSITIVE_AGENT_TOOL_PERMISSIONS: dict[str, Permission] = {
    "publish_content": Permission.CONTENT_PUBLISH,
    "send_direct_message": Permission.INBOX_REPLY,
    "hide_comment": Permission.COMMENTS_HIDE,
    "delete_comment": Permission.COMMENTS_HIDE,
    "update_settings": Permission.SETTINGS_WRITE,
    "change_subscription": Permission.BILLING_MANAGE,
    "approve_agent_tool": Permission.AGENT_APPROVE_TOOL,
}
