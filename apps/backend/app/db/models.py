"""Import every model so ``Base.metadata`` is fully populated.

Alembic autogeneration and ``create_all`` both rely on this module being
imported. Keeping it explicit (rather than relying on import side effects
elsewhere) is what stops a "table not found" surprise in migrations.
"""

from __future__ import annotations

from app.db.base import Base
from app.modules.audit.models import AuditLog, Notification
from app.modules.billing.models import (
    Coupon,
    Invoice,
    Payment,
    Plan,
    Subscription,
    UsageRecord,
)
from app.modules.content.models import (
    AgentRun,
    AgentToolCall,
    AIGeneration,
    AITask,
    BrandProfile,
    Campaign,
    Content,
    ContentApproval,
    ContentCalendarEvent,
    ContentCategory,
    ContentVersion,
    Hashtag,
    MediaAsset,
)
from app.modules.identity.models import (
    EmailVerificationToken,
    OAuthIdentity,
    PasswordResetToken,
    RefreshToken,
    SecurityEvent,
    User,
    UserSession,
)
from app.modules.inbox.models import (
    AnalyticsSnapshot,
    Comment,
    CommentReply,
    Conversation,
    Customer,
    CustomerActivity,
    CustomerNote,
    Message,
)
from app.modules.instagram.models import (
    MetaAPIUsage,
    MetaWebhookEvent,
    OAuthState,
    OAuthToken,
    SocialAccount,
)
from app.modules.publishing.models import PublishingAttempt, PublishingJob, PublishingQuota
from app.modules.tenants.models import (
    NotificationPreference,
    Role,
    SystemSetting,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMembership,
)

ALL_MODELS = [
    User,
    OAuthIdentity,
    UserSession,
    RefreshToken,
    EmailVerificationToken,
    PasswordResetToken,
    SecurityEvent,
    Workspace,
    Role,
    WorkspaceMembership,
    WorkspaceInvitation,
    SystemSetting,
    NotificationPreference,
    SocialAccount,
    OAuthToken,
    OAuthState,
    MetaWebhookEvent,
    MetaAPIUsage,
    MediaAsset,
    BrandProfile,
    Campaign,
    ContentCategory,
    Hashtag,
    Content,
    ContentVersion,
    ContentApproval,
    ContentCalendarEvent,
    AIGeneration,
    AITask,
    AgentRun,
    AgentToolCall,
    PublishingJob,
    PublishingAttempt,
    PublishingQuota,
    Conversation,
    Message,
    Comment,
    CommentReply,
    Customer,
    CustomerNote,
    CustomerActivity,
    AnalyticsSnapshot,
    AuditLog,
    Notification,
    Plan,
    Subscription,
    Coupon,
    UsageRecord,
    Invoice,
    Payment,
]

__all__ = ["ALL_MODELS", "Base"]
