"""Policy-gated execution of typed internal tools."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from pydantic import BaseModel, ValidationError

from app.agent import AgentProposal, ToolCall, ToolData, ToolExecutionResult
from app.policy import (
    PolicyEngine,
    PolicyInput,
    TrustedToolRuntimeContext,
)
from app.policy.models import proposal_value_is_present
from app.tenants.config import FieldType, RoutingDecision
from app.tools.models import (
    CreateCaseArgs,
    CreateReplyDraftArgs,
    CustomerLookupResult,
    CustomerSummary,
    FindCustomerArgs,
    FlagForReviewArgs,
    PendingAction,
    ReplyDraft,
    ReviewFlag,
    ToolDefinition,
    UpdateCaseFieldsArgs,
)
from app.tools.ports import CustomerNotFoundError, TenantToolPort


def _known_proposal_fields(
    proposal: AgentProposal, runtime: TrustedToolRuntimeContext
) -> dict[str, ToolData]:
    known = set(runtime.tenant_config.fields)
    return {
        item.name: deepcopy(item.value)
        for item in proposal.fields
        if item.name in known and proposal_value_is_present(item.value)
    }


def _customer_data(summary: CustomerSummary | None) -> ToolData:
    return CustomerLookupResult(
        found=summary is not None,
        customer=summary,
    ).model_dump(mode="json")


def _recomputed_missing_required_fields(
    proposal: AgentProposal, runtime: TrustedToolRuntimeContext
) -> tuple[str, ...]:
    intake = next(
        (
            item
            for item in runtime.tenant_config.intake_types
            if item.name == proposal.intake_type
        ),
        None,
    )
    if intake is None:
        return ()
    present = set(_known_proposal_fields(proposal, runtime))
    return tuple(sorted(set(intake.required_fields) - present))


class _ToolExecutionStop(RuntimeError):
    """Internal control flow for a safe, stable terminal tool outcome."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


class PolicyGatedToolExecutor:
    """Apply policy before invoking one registered typed tool."""

    def __init__(
        self,
        *,
        runtime: TrustedToolRuntimeContext,
        port: TenantToolPort,
        policy: PolicyEngine | None = None,
    ) -> None:
        self._runtime = runtime
        self._port = port
        self._policy = policy or PolicyEngine()
        self._definitions = self._build_definitions()

    @property
    def definitions(self) -> Mapping[str, ToolDefinition]:
        return self._definitions.copy()

    def _build_definitions(self) -> dict[str, ToolDefinition]:
        return {
            "find_customer": ToolDefinition(
                name="find_customer",
                requires_complete_fields=False,
                arguments_model=FindCustomerArgs,
                handler=self._find_customer,
            ),
            "create_case": ToolDefinition(
                name="create_case",
                requires_complete_fields=True,
                arguments_model=CreateCaseArgs,
                handler=self._create_case,
            ),
            "update_case_fields": ToolDefinition(
                name="update_case_fields",
                requires_complete_fields=True,
                arguments_model=UpdateCaseFieldsArgs,
                handler=self._update_case_fields,
            ),
            "create_reply_draft": ToolDefinition(
                name="create_reply_draft",
                requires_complete_fields=False,
                arguments_model=CreateReplyDraftArgs,
                handler=self._create_reply_draft,
            ),
            "flag_for_review": ToolDefinition(
                name="flag_for_review",
                requires_complete_fields=False,
                arguments_model=FlagForReviewArgs,
                handler=self._flag_for_review,
            ),
        }

    def execute(
        self, proposal: AgentProposal, tool_call: ToolCall
    ) -> ToolExecutionResult:
        definition = self._definitions.get(tool_call.name)
        policy = self._policy.evaluate(
            PolicyInput(
                proposal=proposal,
                requested_action=tool_call.name,
                requires_complete_fields=(
                    definition.requires_complete_fields if definition else False
                ),
                registered_actions=frozenset(self._definitions),
                runtime=self._runtime,
            )
        )
        if policy.decision is not RoutingDecision.ALLOW:
            if (
                definition is None
                or policy.decision is not RoutingDecision.NEEDS_APPROVAL
            ):
                return ToolExecutionResult(
                    data=policy.as_tool_data(),
                    continue_run=False,
                    final_response=policy.reason,
                )
        if definition is None:
            raise AssertionError("allowed action must have a registered tool")
        try:
            arguments = definition.arguments_model.model_validate(
                dict(tool_call.arguments)
            )
        except ValidationError:
            return ToolExecutionResult(
                data={"outcome": "tool_arguments_invalid"},
                continue_run=False,
                final_response="tool_arguments_invalid",
            )
        if policy.decision is RoutingDecision.NEEDS_APPROVAL:
            requested = self._port.request_approval(
                self._runtime.tenant_id,
                action=self._pending_action(definition, arguments, proposal),
                policy_reason=policy.reason,
            )
            data = policy.as_tool_data()
            data["approval_id"] = str(requested.id)
            return ToolExecutionResult(
                data=data,
                continue_run=False,
                final_response="approval_requested",
            )
        try:
            result = definition.handler(arguments, proposal, self._runtime)
            return ToolExecutionResult(data=result)
        except _ToolExecutionStop as error:
            return ToolExecutionResult(
                data={"outcome": error.outcome},
                continue_run=False,
                final_response=error.outcome,
            )
        except CustomerNotFoundError:
            return ToolExecutionResult(
                data={"outcome": "customer_not_found"},
                continue_run=False,
                final_response="customer_not_found",
            )
        except Exception:
            # A tool adapter is a trust boundary.  Never expose backend exception
            # text to the transcript or let an implementation failure escape the loop.
            return ToolExecutionResult(
                data={"outcome": "tool_execution_failed"},
                continue_run=False,
                final_response="tool_execution_failed",
            )

    def _pending_action(
        self,
        definition: ToolDefinition,
        arguments: BaseModel,
        proposal: AgentProposal,
    ) -> PendingAction:
        return PendingAction(
            name=definition.name,
            arguments=deepcopy(arguments.model_dump(mode="json")),
            known_fields=deepcopy(_known_proposal_fields(proposal, self._runtime)),
        )

    def _find_customer(
        self,
        arguments: BaseModel,
        proposal: AgentProposal,
        runtime: TrustedToolRuntimeContext,
    ) -> ToolData:
        values = FindCustomerArgs.model_validate(arguments)
        return _customer_data(
            self._port.find_customer(
                runtime.tenant_id,
                email=values.email,
                external_id=values.external_id,
            )
        )

    def _create_case(
        self,
        arguments: BaseModel,
        proposal: AgentProposal,
        runtime: TrustedToolRuntimeContext,
    ) -> ToolData:
        values = CreateCaseArgs.model_validate(arguments)
        if runtime.source is None:
            raise _ToolExecutionStop("trusted_source_unavailable")
        result = self._port.create_case(
            runtime.tenant_id,
            runtime.source,
            customer_id=values.customer_id,
            fields=_known_proposal_fields(proposal, runtime),
        )
        return result.model_dump(mode="json")

    def _update_case_fields(
        self,
        arguments: BaseModel,
        proposal: AgentProposal,
        runtime: TrustedToolRuntimeContext,
    ) -> ToolData:
        values = UpdateCaseFieldsArgs.model_validate(arguments)
        result = self._port.update_case_fields(
            runtime.tenant_id,
            values.case_id,
            fields=_known_proposal_fields(proposal, runtime),
        )
        if result is None:
            raise _ToolExecutionStop("case_not_found")
        return result.model_dump(mode="json")

    def _create_reply_draft(
        self,
        arguments: BaseModel,
        proposal: AgentProposal,
        runtime: TrustedToolRuntimeContext,
    ) -> ToolData:
        values = CreateReplyDraftArgs.model_validate(arguments)
        if values.case_id is not None and not self._port.case_exists(
            runtime.tenant_id, values.case_id
        ):
            raise _ToolExecutionStop("case_not_found")
        contact_definition = runtime.tenant_config.fields.get("contact")
        recipient_value = next(
            (
                item.value
                for item in proposal.fields
                if (
                    item.name == "contact"
                    and contact_definition is not None
                    and contact_definition.type is FieldType.EMAIL
                    and isinstance(item.value, str)
                )
            ),
            None,
        )
        missing = ", ".join(_recomputed_missing_required_fields(proposal, runtime))
        body = proposal.rationale_short
        if missing:
            body += f" Missing information: {missing}."
        subject = runtime.source.subject if runtime.source else "Intake reply draft"
        return ReplyDraft(
            recipient=recipient_value,
            subject=subject,
            body=body,
            persisted=False,
        ).model_dump(mode="json")

    def _flag_for_review(
        self,
        arguments: BaseModel,
        proposal: AgentProposal,
        runtime: TrustedToolRuntimeContext,
    ) -> ToolData:
        values = FlagForReviewArgs.model_validate(arguments)
        return ReviewFlag(
            reason=values.note or proposal.rationale_short,
            persisted=False,
        ).model_dump(mode="json")


__all__ = ["PolicyGatedToolExecutor"]
