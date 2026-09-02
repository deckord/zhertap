from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import account_has_active_trial, account_has_paid_access
from app.models import Account, AuctionWorkspace, AuctionWorkspaceMember

OBSERVER_PLAN = "observer"
INVESTOR_PLAN = "investor"
TEAM_PLAN = "team"
LITE_PLAN = "lite"
PRO_PLAN = "pro"
PRO_YEAR_PLAN = "pro_year"

PLAN_LABELS = {
    OBSERVER_PLAN: "Наблюдатель",
    INVESTOR_PLAN: "Инвестор Pro",
    TEAM_PLAN: "Команда",
    LITE_PLAN: "Жертап Lite",
    PRO_PLAN: "Жертап PRO",
    PRO_YEAR_PLAN: "Жертап PRO годовой",
}

TEAM_ROLES = (
    ("manager", "Управляющий"),
    ("analyst", "Аналитик"),
    ("viewer", "Наблюдатель"),
)
TEAM_ROLE_LABELS = {
    "owner": "Владелец",
    **dict(TEAM_ROLES),
}


@dataclass(frozen=True, slots=True)
class AuctionEntitlement:
    plan: str
    label: str
    can_open_detail: bool
    can_use_map: bool
    can_use_analytics: bool
    can_use_portfolio: bool
    can_monitor: bool
    can_edit: bool
    can_manage_team: bool
    is_admin: bool = False

    def as_context(self) -> dict[str, object]:
        return {
            "plan": self.plan,
            "label": self.label,
            "can_open_detail": self.can_open_detail,
            "can_use_map": self.can_use_map,
            "can_use_analytics": self.can_use_analytics,
            "can_use_portfolio": self.can_use_portfolio,
            "can_monitor": self.can_monitor,
            "can_edit": self.can_edit,
            "can_manage_team": self.can_manage_team,
            "is_admin": self.is_admin,
        }


@dataclass(frozen=True, slots=True)
class AuctionDataScope:
    account_id: str
    workspace: AuctionWorkspace | None
    member: AuctionWorkspaceMember | None

    @property
    def role(self) -> str:
        return self.member.role if self.member else "owner"


def _active_membership(
    session: Session, account_id: str
) -> AuctionWorkspaceMember | None:
    return session.scalar(
        select(AuctionWorkspaceMember)
        .join(AuctionWorkspace, AuctionWorkspace.id == AuctionWorkspaceMember.workspace_id)
        .where(
            AuctionWorkspaceMember.account_id == account_id,
            AuctionWorkspaceMember.status == "active",
            AuctionWorkspace.active.is_(True),
        )
        .order_by(AuctionWorkspaceMember.id)
        .limit(1)
    )


def _team_membership_is_funded(
    session: Session, membership: AuctionWorkspaceMember | None
) -> bool:
    if membership is None:
        return False
    workspace = session.get(AuctionWorkspace, membership.workspace_id)
    if workspace is None or not workspace.active:
        return False
    owner = session.get(Account, workspace.owner_account_id)
    return bool(
        owner
        and owner.auction_plan == TEAM_PLAN
        and account_has_paid_access(owner)
    )


def effective_auction_plan(
    session: Session,
    account: Account,
    *,
    is_admin: bool = False,
) -> str:
    if is_admin:
        return TEAM_PLAN
    membership = _active_membership(session, account.id)
    if _team_membership_is_funded(session, membership):
        return TEAM_PLAN
    if account_has_paid_access(account):
        if account.auction_plan == TEAM_PLAN:
            return TEAM_PLAN
        if account.auction_plan in {LITE_PLAN, "lite"}:
            return OBSERVER_PLAN
        return INVESTOR_PLAN
    if account_has_active_trial(account):
        return INVESTOR_PLAN
    return OBSERVER_PLAN


def auction_entitlement(
    session: Session,
    account: Account,
    *,
    is_admin: bool = False,
) -> AuctionEntitlement:
    plan = effective_auction_plan(session, account)
    paid_features = is_admin or plan in {INVESTOR_PLAN, TEAM_PLAN}
    membership = _active_membership(session, account.id) if plan == TEAM_PLAN else None
    role = membership.role if membership else "owner"
    can_edit = paid_features and role != "viewer"
    return AuctionEntitlement(
        plan=plan,
        label=PLAN_LABELS[plan],
        can_open_detail=paid_features,
        can_use_map=paid_features,
        can_use_analytics=paid_features,
        can_use_portfolio=paid_features,
        can_monitor=paid_features,
        can_edit=can_edit,
        can_manage_team=plan == TEAM_PLAN and role in {"owner", "manager"},
        is_admin=is_admin,
    )


def auction_data_scope(
    session: Session,
    account: Account,
    *,
    is_admin: bool = False,
) -> AuctionDataScope:
    plan = effective_auction_plan(session, account, is_admin=is_admin)
    if plan != TEAM_PLAN:
        return AuctionDataScope(account_id=account.id, workspace=None, member=None)
    membership = _active_membership(session, account.id)
    if not _team_membership_is_funded(session, membership):
        return AuctionDataScope(account_id=account.id, workspace=None, member=None)
    workspace = session.get(AuctionWorkspace, membership.workspace_id)
    if workspace is None:
        return AuctionDataScope(account_id=account.id, workspace=None, member=None)
    return AuctionDataScope(
        account_id=workspace.owner_account_id,
        workspace=workspace,
        member=membership,
    )


def ensure_team_workspace(session: Session, account: Account) -> AuctionWorkspace:
    if account.auction_plan != TEAM_PLAN or not account_has_paid_access(account):
        raise ValueError("Тариф «Команда» не активен")
    existing = session.scalar(
        select(AuctionWorkspace)
        .where(
            AuctionWorkspace.owner_account_id == account.id,
            AuctionWorkspace.active.is_(True),
        )
        .limit(1)
    )
    if existing is not None:
        return existing
    workspace = AuctionWorkspace(
        name=f"Команда {account.phone[-4:]}",
        owner_account_id=account.id,
    )
    session.add(workspace)
    session.flush()
    session.add(
        AuctionWorkspaceMember(
            workspace_id=workspace.id,
            account_id=account.id,
            role="owner",
            status="active",
            invited_by_account_id=account.id,
        )
    )
    session.flush()
    return workspace


def list_workspace_members(
    session: Session, workspace_id: str
) -> list[AuctionWorkspaceMember]:
    return list(
        session.scalars(
            select(AuctionWorkspaceMember)
            .where(AuctionWorkspaceMember.workspace_id == workspace_id)
            .order_by(AuctionWorkspaceMember.id)
        ).all()
    )


def add_workspace_member(
    session: Session,
    *,
    workspace: AuctionWorkspace,
    invited_by: Account,
    account: Account,
    role: str,
    max_members: int = 5,
) -> AuctionWorkspaceMember:
    if role not in dict(TEAM_ROLES):
        raise ValueError("Неизвестная роль")
    existing = session.scalar(
        select(AuctionWorkspaceMember).where(
            AuctionWorkspaceMember.workspace_id == workspace.id,
            AuctionWorkspaceMember.account_id == account.id,
        )
    )
    if existing is not None:
        existing.role = role if existing.role != "owner" else "owner"
        existing.status = "active"
        return existing
    other_membership = _active_membership(session, account.id)
    if other_membership is not None and other_membership.workspace_id != workspace.id:
        raise ValueError("Пользователь уже состоит в другой команде")
    active_count = len(
        list_workspace_members(session, workspace.id)
    )
    if active_count >= max_members:
        raise ValueError(f"На тарифе доступно не более {max_members} участников")
    member = AuctionWorkspaceMember(
        workspace_id=workspace.id,
        account_id=account.id,
        role=role,
        status="active",
        invited_by_account_id=invited_by.id,
    )
    session.add(member)
    session.flush()
    return member


def deactivate_workspace_member(
    session: Session,
    *,
    workspace_id: str,
    member_id: int,
) -> AuctionWorkspaceMember | None:
    member = session.scalar(
        select(AuctionWorkspaceMember).where(
            AuctionWorkspaceMember.id == member_id,
            AuctionWorkspaceMember.workspace_id == workspace_id,
        )
    )
    if member is None or member.role == "owner":
        return None
    member.status = "inactive"
    session.flush()
    return member
