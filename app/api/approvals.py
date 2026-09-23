"""Operator-only approval decision endpoint."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedOperator, get_current_operator
from app.db.session import get_db_session
from app.domain.approvals import (
    ApprovalDecisionConflict,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalDecisionService,
    ApprovalExpired,
    ApprovalNotFound,
)
from app.tools.postgres import PostgresApprovalActionExecutor


router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.post(
    "/{approval_id}/decide",
    response_model=ApprovalDecisionResponse,
)
async def decide_approval(
    approval_id: UUID,
    request: ApprovalDecisionRequest,
    operator: Annotated[AuthenticatedOperator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_db_session, use_cache=False)],
) -> ApprovalDecisionResponse:
    try:
        return await ApprovalDecisionService(
            session,
            PostgresApprovalActionExecutor(),
        ).decide(approval_id, operator, request)
    except ApprovalNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval not found",
        ) from error
    except ApprovalExpired as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Approval expired",
        ) from error
    except ApprovalDecisionConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Approval decision conflict",
        ) from error


__all__ = ["router"]
