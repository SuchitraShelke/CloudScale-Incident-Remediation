# ADR-007: Cloud deployment target: AWS EKS primary, Azure AKS as a portability overlay

**Status:** Accepted (designed, not deployed in the capstone) · **Rubric:** Engineering Package (cloud deployment targets)

## Context
CloudScale runs production on AWS and Azure. The platform itself needs one primary home with a clear path to the other, and the TCO needs one costed target.

## Decision
- **Primary: AWS EKS** in `us-east-1`.
  - App node group: 3 → 5 × m6i.large
  - Dedicated guard node (c6i.xlarge) for Llama Guard
  - RDS PostgreSQL (Multi-AZ, pgvector) and ElastiCache Redis
  - An Istio/Linkerd mesh for mTLS on every internal hop, including OPA and the guard
- **Overlay: Azure AKS**, using the same containers with Azure Database for PostgreSQL and Azure Cache for Redis. It's costed only as a warm-DR sensitivity case.
- **Same shape as the prototype:** the 3 app containers and 7 infrastructure components map 1:1 to Deployments, with NetworkPolicies mirroring the Docker networks. OPA runs as a sidecar of the MCP server.
- **Secrets:** the Anthropic key and the Ed25519 signing key come from AWS Secrets Manager / Azure Key Vault through External Secrets. The signing key lives only in a separate approval service (see ADR-005).

## Alternatives considered
| Option | Why not (now) |
|---|---|
| Both clouds active-active | Doubles infra cost with no stated need; v1 did this without justification |
| Serverless (Lambda / Container Apps) | LangGraph interrupts wait on humans for minutes to hours; long-lived workers plus a checkpointer fit better |
| VMs without Kubernetes | Loses NetworkPolicies, HPA and the mesh, which are the zero-trust controls |

## Consequences
- The TCO costs EKS only; AKS warm DR is a +$500/month sensitivity line.
- The Docker Compose stack is the *validated local reference*. Cloud manifests are future work and aren't claimed as built.

**See:** `docs/financial/tco-roi-model.md` (infra lines), `docs/architecture/01-c4-container.md`.
