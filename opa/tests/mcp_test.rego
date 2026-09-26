# One allow case plus at least one deny case per rule: `opa test opa/policies opa/data opa/tests -v`
package cloudscale.mcp_test

import data.cloudscale.mcp

scope := {"namespace": "production", "cloud_provider": "aws"}

counters := {"calls_last_minute": 1, "breaker_state": "CLOSED"}

read_input := {
	"tool": "get_metrics", "op_class": "READ", "args_hash": "h0",
	"args": {"scope": scope, "service": "orders-service"},
	"token": {"typ": "read", "namespace": "production", "cloud_provider": "aws"},
	"counters": counters,
}

exec_input := {
	"tool": "apply_hotfix", "op_class": "DESTRUCTIVE", "args_hash": "h1",
	"args": {"scope": scope, "deployment": "orders-service"},
	"token": {
		"typ": "exec", "namespace": "production", "cloud_provider": "aws",
		"step": {"tool": "apply_hotfix", "op_class": "DESTRUCTIVE", "args_hash": "h1"},
		"approval": {"gate": "APPROVAL", "approver": "sre1"},
	},
	"counters": counters,
}

denies(inp, reason) if reason in mcp.deny with input as inp

test_read_allowed if mcp.allow with input as read_input

test_approved_destructive_allowed if mcp.allow with input as exec_input

test_empty_input_denied if not mcp.allow with input as {}

test_unknown_tool if denies(object.union(read_input, {"tool": "delete_cluster"}), "unknown_tool")

test_bad_token_type if denies(object.union(read_input, {"token": {"typ": "admin"}}), "bad_token_type")

test_namespace_scope if denies(
	object.union(read_input, {"args": {"scope": {"namespace": "kube-system", "cloud_provider": "aws"}}}),
	"namespace_scope",
)

test_cloud_scope if denies(
	object.union(read_input, {"args": {"scope": {"namespace": "production", "cloud_provider": "azure"}}}),
	"cloud_scope",
)

test_wildcard_nested if denies(
	object.union(read_input, {"args": {"scope": scope, "service": "orders-*"}}),
	"wildcard_arg",
)

test_read_token_cannot_mutate if denies(
	object.union(read_input, {"tool": "clear_pod_cache", "op_class": "SAFE_MUTATION"}),
	"read_token_mutation",
)

test_exec_tool_mismatch if denies(object.union(exec_input, {"tool": "rollback_deployment"}), "exec_tool_mismatch")

test_exec_args_mismatch if denies(object.union(exec_input, {"args_hash": "tampered"}), "exec_args_mismatch")

test_op_class_escalated if denies(
	object.union(exec_input, {"token": object.union(exec_input.token, {"step": {
		"tool": "apply_hotfix", "op_class": "SAFE_MUTATION", "args_hash": "h1",
	}})}),
	"op_class_escalated",
)

test_destructive_on_auto_denied if denies(
	object.union(exec_input, {"token": object.union(exec_input.token, {"approval": {
		"gate": "AUTO", "approver": "system",
	}})}),
	"missing_approval",
)

# object.union merges recursively, so drop the key first instead of overriding it.
test_destructive_without_approval_denied if denies(
	object.union(object.remove(exec_input, ["token"]), {"token": object.remove(exec_input.token, ["approval"])}),
	"missing_approval",
)

test_missing_scope_is_malformed if denies(object.union(object.remove(read_input, ["args"]), {"args": {}}), "malformed_input")

test_rate_limited if denies(
	object.union(read_input, {"counters": {"calls_last_minute": 999, "breaker_state": "CLOSED"}}),
	"rate_limited",
)

test_circuit_open if denies(
	object.union(read_input, {"counters": {"calls_last_minute": 1, "breaker_state": "OPEN"}}),
	"circuit_open",
)
