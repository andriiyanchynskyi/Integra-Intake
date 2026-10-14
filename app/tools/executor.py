"""Policy-gated execution of typed internal tools."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from app.agent import (
    AgentProposal,
    ResultReference,
    ToolCall,
    ToolData,
    ToolExecutionDisposition,
    ToolExecutionEvidence,
    ToolExecutionResult,
)
from app.policy import (
    PolicyEngine,
    PolicyInput,
    PolicyOutcome,
    TrustedToolRuntimeContext,
)
from app.tenants.config import FieldType, RoutingDecision
from app.tenants.field_values import (
    ValidatedProposalFields,
    validate_proposal_fields,
)
from app.observability import (
    Component,
    EventName,
    NULL_OBSERVER,
    ObservationContext,
    ObservationEvent,
    Observer,
    OutcomeCode,
    PolicyReason,
    safe_emit,
)
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
from app.tools.registry import ActionRegistry, BUILTIN_ACTION_REGISTRY


def _validated_proposal_fields(
    proposal: AgentProposal, runtime: TrustedToolRuntimeContext
) -> ValidatedProposalFields:
    return validate_proposal_fields(proposal, runtime)


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
    present = set(_validated_proposal_fields(proposal, runtime).values)
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
        observer: Observer = NULL_OBSERVER,
        context: ObservationContext | None = None,
        clock: Callable[[], int] = time.perf_counter_ns,
        action_registry: ActionRegistry = BUILTIN_ACTION_REGISTRY,
    ) -> None:
        self._runtime = runtime
        self._port = port
        self._policy = policy or PolicyEngine()
        self._observer = observer
        self._action_registry = action_registry
        self._context = (
            context
            or ObservationContext(
                trace_id=uuid4(),
                tenant_id=runtime.tenant_id,
                job_id=runtime.job_id,
            )
        ).bind(tenant_id=runtime.tenant_id, job_id=runtime.job_id)
        self._clock = clock
        self._definitions = self._build_definitions()

    @property
    def definitions(self) -> Mapping[str, ToolDefinition]:
        return self._definitions.copy()

    def _build_definitions(self) -> dict[str, ToolDefinition]:
        handlers = {
            "find_customer": self._find_customer,
            "create_case": self._create_case,
            "update_case_fields": self._update_case_fields,
            "create_reply_draft": self._create_reply_draft,
            "flag_for_review": self._flag_for_review,
        }
        if frozenset(handlers) != self._action_registry.keys:
            raise RuntimeError("action registry and handlers are incompatible")
        definitions: dict[str, ToolDefinition] = {}
        for key, handler in handlers.items():
            capability = self._action_registry.require(key)
            definitions[key] = ToolDefinition(
                name=capability.key,
                requires_complete_fields=capability.requires_complete_fields,
                arguments_model=capability.arguments_model,
                handler=handler,
                commits_side_effect=capability.commits_side_effect,
                pending_action_version=capability.pending_action_version,
            )
        return definitions

    @classmethod
    def _with_evidence(
        cls,
        result: ToolExecutionResult,
        *,
        action_key: str,
        action_known: bool,
        policy: PolicyOutcome,
        tool_executed: bool,
        side_effect_committed: bool = False,
    ) -> ToolExecutionResult:
        return replace(
            result,
            result_reference=cls._result_reference(result.data),
            evidence=ToolExecutionEvidence(
                action_key=action_key if action_known else None,
                action_known=action_known,
                policy_decision=policy.decision.value,
                routing_status=policy.status.value,
                policy_reason=policy.reason,
                missing_required_fields=policy.missing_required_fields,
                tool_outcome=(
                    cls._tool_outcome(result.data).value
                    if tool_executed
                    else None
                ),
                side_effect_committed=side_effect_committed,
            ),
        )

    @staticmethod
    def _result_reference(value: ToolData) -> ResultReference | None:
        if not isinstance(value, Mapping):
            return None
        raw = value.get("result_reference")
        if not isinstance(raw, Mapping):
            return None
        try:
            return ResultReference(
                kind=raw["kind"],  # type: ignore[arg-type]
                id=UUID(raw["id"]) if isinstance(raw["id"], str) else raw["id"],  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            return None

    def execute(
        self, proposal: AgentProposal, tool_call: ToolCall
    ) -> ToolExecutionResult:
        self._check_cancelled()
        started_ns = self._clock()
        definition = self._definitions.get(tool_call.name)
        validated_fields = _validated_proposal_fields(proposal, self._runtime)
        policy = self._policy.evaluate(
            PolicyInput(
                proposal=proposal,
                requested_action=tool_call.name,
                requires_complete_fields=(
                    definition.requires_complete_fields if definition else False
                ),
                registered_actions=(
                    self._runtime.compiled_profile.registered_actions
                    if self._runtime.compiled_profile is not None
                    else frozenset(self._definitions)
                ),
                runtime=self._runtime,
                verified_present_fields=frozenset(validated_fields.values),
                invalid_fields=validated_fields.invalid_fields,
            )
        )
        self._emit_policy(
            action_key=tool_call.name,
            action_known=definition is not None,
            policy=policy,
            started_ns=started_ns,
        )
        if policy.decision is not RoutingDecision.ALLOW:
            if (
                definition is None
                or policy.decision is not RoutingDecision.NEEDS_APPROVAL
            ):
                result = ToolExecutionResult(
                    data=policy.as_tool_data(),
                    continue_run=False,
                    final_response=policy.reason,
                )
                return self._with_evidence(
                    result,
                    action_key=tool_call.name,
                    action_known=definition is not None,
                    policy=policy,
                    tool_executed=False,
                )
        if definition is None:
            raise AssertionError("allowed action must have a registered tool")
        try:
            arguments = definition.arguments_model.model_validate(
                dict(tool_call.arguments)
            )
        except ValidationError:
            result = ToolExecutionResult(
                data={"outcome": "tool_arguments_invalid"},
                continue_run=False,
                final_response="tool_arguments_invalid",
            )
            self._emit_tool(
                action_key=definition.name,
                result=result,
                started_ns=started_ns,
                side_effect_committed=False,
            )
            return self._with_evidence(
                result,
                action_key=definition.name,
                action_known=True,
                policy=policy,
                tool_executed=True,
            )
        if policy.decision is RoutingDecision.NEEDS_APPROVAL:
            self._check_cancelled()
            requested = self._port.request_approval(
                self._runtime.tenant_id,
                action=self._pending_action(definition, arguments, proposal),
                policy_reason=policy.reason,
            )
            self._emit_approval_requested(
                action_key=definition.name,
                approval_id=requested.id,
                policy_reason=policy.reason,
            )
            data = policy.as_tool_data()
            data["approval_id"] = str(requested.id)
            result = ToolExecutionResult(
                data=data,
                continue_run=False,
                final_response="approval_requested",
            )
            return self._with_evidence(
                result,
                action_key=definition.name,
                action_known=True,
                policy=policy,
                tool_executed=False,
            )
        try:
            self._check_cancelled()
            result = definition.handler(arguments, proposal, self._runtime)
            execution = ToolExecutionResult(data=result)
            self._emit_tool(
                action_key=definition.name,
                result=execution,
                started_ns=started_ns,
                side_effect_committed=definition.commits_side_effect,
            )
            return self._with_evidence(
                execution,
                action_key=definition.name,
                action_known=True,
                policy=policy,
                tool_executed=True,
                side_effect_committed=definition.commits_side_effect,
            )
        except _ToolExecutionStop as error:
            execution = ToolExecutionResult(
                data={"outcome": error.outcome},
                continue_run=False,
                final_response=error.outcome,
            )
            self._emit_tool(
                action_key=definition.name,
                result=execution,
                started_ns=started_ns,
                side_effect_committed=False,
            )
            return self._with_evidence(
                execution,
                action_key=definition.name,
                action_known=True,
                policy=policy,
                tool_executed=True,
            )

        except CustomerNotFoundError:
            execution = ToolExecutionResult(
                data={"outcome": "customer_not_found"},
                continue_run=False,
                final_response="customer_not_found",
            )
            self._emit_tool(
                action_key=definition.name,
                result=execution,
                started_ns=started_ns,
                side_effect_committed=False,
            )
            return self._with_evidence(
                execution,
                action_key=definition.name,
                action_known=True,
                policy=policy,
                tool_executed=True,
            )

        except Exception:
            # A tool adapter is a trust boundary.  Never expose backend exception
            # text to the transcript or let an implementation failure escape the loop.
            execution = ToolExecutionResult(
                data={"outcome": "tool_execution_failed"},
                disposition=ToolExecutionDisposition.FAILED,
                final_response="tool_execution_failed",
            )
            self._emit_tool(
                action_key=definition.name,
                result=execution,
                started_ns=started_ns,
                side_effect_committed=False,
            )
            return self._with_evidence(
                execution,
                action_key=definition.name,
                action_known=True,
                policy=policy,
                tool_executed=True,
            )

    def evaluate_terminal(self, proposal: AgentProposal) -> ToolExecutionResult:
        """Evaluate a terminal proposal without inventing an executable action."""

        self._check_cancelled()
        started_ns = self._clock()
        validated_fields = _validated_proposal_fields(proposal, self._runtime)
        policy = self._policy.evaluate_terminal(
            PolicyInput(
                proposal=proposal,
                requested_action="",
                requires_complete_fields=True,
                registered_actions=(
                    self._runtime.compiled_profile.registered_actions
                    if self._runtime.compiled_profile is not None
                    else frozenset(self._definitions)
                ),
                runtime=self._runtime,
                verified_present_fields=frozenset(validated_fields.values),
                invalid_fields=validated_fields.invalid_fields,
            )
        )
        self._emit_policy(
            action_key="",
            action_known=False,
            policy=policy,
            started_ns=started_ns,
        )
        data = policy.as_tool_data()
        data["business_outcome"] = "routing_only"
        final_response = (
            "review_required"
            if policy.reason == "safety_or_legal_risk"
            else policy.reason
        )
        result = ToolExecutionResult(
            data=data,
            continue_run=False,
            final_response=final_response,
        )
        return self._with_evidence(
            result,
            action_key="",
            action_known=False,
            policy=policy,
            tool_executed=False,
        )

    def _emit_policy(
        self,
        *,
        action_key: str,
        action_known: bool,
        policy: object,
        started_ns: int,
    ) -> None:
        """Emit only closed policy values; the policy engine remains pure."""

        decision = policy.decision  # type: ignore[attr-defined]
        outcome = {
            RoutingDecision.ALLOW: OutcomeCode.ALLOW,
            RoutingDecision.DENY: OutcomeCode.DENY,
            RoutingDecision.NEEDS_APPROVAL: OutcomeCode.NEEDS_APPROVAL,
        }[decision]
        try:
            safe_reason = PolicyReason(policy.reason)  # type: ignore[attr-defined]
        except ValueError:
            safe_reason = None
        safe_emit(
            self._observer,
            ObservationEvent(
                event=EventName.POLICY_EVALUATED,
                trace_id=self._context.trace_id,
                request_id=self._context.request_id,
                tenant_id=self._context.tenant_id,
                job_id=self._context.job_id,
                component=Component.POLICY,
                scenario_key=self._context.scenario_key,
                profile_fingerprint=self._context.profile_fingerprint,
                outcome=outcome,
                duration_ms=self._duration_ms(started_ns),
                action_key=action_key if action_known else None,
                action_known=action_known,
                policy_decision=decision,
                routing_status=policy.status,  # type: ignore[attr-defined]
                policy_reason=safe_reason,
                missing_fields=policy.missing_required_fields,  # type: ignore[attr-defined]
            ),
        )

    def _emit_tool(
        self,
        *,
        action_key: str,
        result: ToolExecutionResult,
        started_ns: int,
        side_effect_committed: bool,
    ) -> None:
        safe_emit(
            self._observer,
            ObservationEvent(
                event=EventName.TOOL_EXECUTION_COMPLETED,
                trace_id=self._context.trace_id,
                request_id=self._context.request_id,
                tenant_id=self._context.tenant_id,
                job_id=self._context.job_id,
                component=Component.TOOL,
                scenario_key=self._context.scenario_key,
                profile_fingerprint=self._context.profile_fingerprint,
                outcome=self._tool_outcome(result.data),
                duration_ms=self._duration_ms(started_ns),
                action_key=action_key,
                action_known=True,
                result_is_none=result.data is None,
                side_effect_committed=side_effect_committed,
            ),
        )

    def _emit_approval_requested(
        self,
        *,
        action_key: str,
        approval_id: object,
        policy_reason: str,
    ) -> None:
        try:
            safe_reason = PolicyReason(policy_reason)
        except ValueError:
            return
        safe_emit(
            self._observer,
            ObservationEvent(
                event=EventName.APPROVAL_REQUESTED,
                trace_id=self._context.trace_id,
                request_id=self._context.request_id,
                tenant_id=self._context.tenant_id,
                job_id=self._context.job_id,
                approval_id=approval_id,
                component=Component.APPROVAL,
                scenario_key=self._context.scenario_key,
                profile_fingerprint=self._context.profile_fingerprint,
                outcome=OutcomeCode.NEEDS_APPROVAL,
                action_key=action_key,
                action_known=True,
                policy_reason=safe_reason,
            ),
        )

    @staticmethod
    def _tool_outcome(value: ToolData) -> OutcomeCode:
        if value is None:
            return OutcomeCode.NO_RESULT
        if isinstance(value, Mapping):
            raw = value.get("outcome")
            if isinstance(raw, str):
                if raw == "tool_arguments_invalid":
                    return OutcomeCode.INVALID_ARGUMENTS
                if raw in {"customer_not_found", "case_not_found"}:
                    return OutcomeCode.NOT_FOUND
                if raw == "tool_execution_failed":
                    return OutcomeCode.BACKEND_FAILURE
                try:
                    return OutcomeCode(raw)
                except ValueError:
                    pass
        return OutcomeCode.EXECUTED

    def _duration_ms(self, started_ns: int) -> int:
        return max(0, (self._clock() - started_ns) // 1_000_000)

    def _check_cancelled(self) -> None:
        event = self._runtime.cancellation_event
        if event is not None and event.is_set():
            raise RuntimeError("worker execution cancelled")

    def _pending_action(
        self,
        definition: ToolDefinition,
        arguments: BaseModel,
        proposal: AgentProposal,
    ) -> PendingAction:
        return PendingAction(
            version=definition.pending_action_version,
            name=definition.name,
            arguments=deepcopy(arguments.model_dump(mode="json")),
            known_fields=deepcopy(
                _validated_proposal_fields(proposal, self._runtime).values
            ),
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
            fields=_validated_proposal_fields(proposal, runtime).values,
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
            fields=_validated_proposal_fields(proposal, runtime).values,
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
        accepted_fields = _validated_proposal_fields(proposal, runtime).values
        recipient_value: str | None = None
        contact_value = accepted_fields.get("contact")
        if (
            contact_definition is not None
            and contact_definition.type is FieldType.EMAIL
            and isinstance(contact_value, str)
        ):
            recipient_value = contact_value
        missing = ", ".join(_recomputed_missing_required_fields(proposal, runtime))
        body = proposal.rationale_short
        if missing:
            body += f" Missing information: {missing}."
        subject = runtime.source.subject if runtime.source else "Intake reply draft"
        reference = self._port.create_reply_draft(
            runtime.tenant_id,
            job_id=runtime.job_id,
            recipient=recipient_value,
            subject=subject,
            body=body,
        )
        return {
            "result_reference": reference.model_dump(mode="json"),
            "persisted": True,
        }

    def _flag_for_review(
        self,
        arguments: BaseModel,
        proposal: AgentProposal,
        runtime: TrustedToolRuntimeContext,
    ) -> ToolData:
        values = FlagForReviewArgs.model_validate(arguments)
        reference = self._port.flag_for_review(
            runtime.tenant_id,
            job_id=runtime.job_id,
            reason=values.note or proposal.rationale_short,
        )
        return {
            "result_reference": reference.model_dump(mode="json"),
            "persisted": True,
        }


__all__ = ["PolicyGatedToolExecutor"]
