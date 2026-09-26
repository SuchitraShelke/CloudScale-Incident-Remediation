# Default-deny policy for every MCP tool call. All deny rules are AND-ed: any one denies.
# Data (op_classes, op_rank, limits) is generated from gate_policy.yaml by scripts/gen_opa_data.py.
package cloudscale.mcp

default allow := false

allow if count(deny) == 0

decision := {"allow": allow, "reasons": deny}

# Rules over missing fields are *undefined* (not true), so without this a malformed input would
# fire no deny rule and be allowed. Every field the rules read must be present.
required := {"tool", "op_class", "args_hash", "args", "token", "counters"}

deny contains "malformed_input" if {
	some field in required
	not field in object.keys(input)
}

deny contains "malformed_input" if not input.args.scope

deny contains "unknown_tool" if not data.op_classes[input.tool]

deny contains "bad_token_type" if not input.token.typ in {"read", "exec"}

deny contains "namespace_scope" if input.args.scope.namespace != input.token.namespace

deny contains "cloud_scope" if input.args.scope.cloud_provider != input.token.cloud_provider

deny contains "wildcard_arg" if {
	some v in strings_in(input.args)
	contains(v, "*")
}

deny contains "read_token_mutation" if {
	input.token.typ == "read"
	input.op_class != "READ"
}

deny contains "exec_tool_mismatch" if {
	input.token.typ == "exec"
	input.tool != input.token.step.tool
}

deny contains "exec_args_mismatch" if {
	input.token.typ == "exec"
	input.args_hash != input.token.step.args_hash
}

# e.g. a token approved for a SAFE_MUTATION used for a DESTRUCTIVE call
deny contains "op_class_escalated" if {
	input.token.typ == "exec"
	data.op_rank[input.op_class] > data.op_rank[input.token.step.op_class]
}

# Destructive operations can never run on an AUTO decision or without a named human approver.
# Written positively and negated: `not x in {...}` is undefined (so never denies) when x is missing.
approved_by_human if {
	input.token.approval.gate in {"APPROVAL", "ESCALATION"}
	input.token.approval.approver != "system"
}

deny contains "missing_approval" if {
	input.op_class == "DESTRUCTIVE"
	not approved_by_human
}

deny contains "rate_limited" if input.counters.calls_last_minute >= data.limits.max_calls_per_minute

deny contains "circuit_open" if input.counters.breaker_state == "OPEN"

strings_in(x) := {v | walk(x, [_, v]); is_string(v)}
