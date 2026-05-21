# Repo Policy Closeout Template

Repo-policy closeout must answer the human operational question first: **where did this work actually land, what did not happen yet, and what decision comes next?**

The policy ledger still matters, but it belongs behind the plain-language answer. A green test suite is not enough if the repo policy is missing, stale, mismatched, or if the report hides whether work reached `develop`, a release PR, live runtime, or only a local worktree.

## Standard human-first template

```text
결론
- <한 줄로: 이 작업이 실제로 어디까지 반영됐는지>

실제 반영
- <작업 결과가 어느 branch/worktree/commit/policy/checker까지 반영됐는지>

아직 안 한 것
- <push/PR/merge/release/deploy/live apply/restart/env-secret/customer-visible mutation 중 하지 않은 것>

다음 판단
- <Chris가 판단해야 할 것 또는 다음 카드/게이트>

Policy check
- <repo-policy checker result, policy path, pass/fail_closed/drift reason>

Green 완료
- <completed local/green work; keep this as ledger, not the headline>

Yellow 대기
- <queued release/live/restart/review items, or none>

Red 필요
- <actions still requiring explicit approval, or none crossed>

검증
- <tests/static checks/proofs>

Git 상태
- <branch/worktree/commit/dirty state/push status>

Live 상태
- <deployed/live/runtime/customer-visible state>
```

## Runtime/tooling extra sections

Hermes-agent and other runtime/tooling repos must also report the live-apply queue state explicitly. These sections do **not** authorize a restart; they prevent restart-needed work from being hidden.

```text
Gateway restart 필요
- <yes/no and why; do not restart unless explicitly approved>

Live runtime 반영됨
- <yes/no with runtime proof if applied>

대기열 포함됨
- <yes/no; queue entry id/details if restart/live apply is pending>
```

## Incomplete closeout rule

A closeout missing `Policy check` is incomplete. The agent must not treat it as final progress, because it hides whether authority came from `.hermes/repo-policy.yaml`, from stale memory, or from an unsafe assumption.

A closeout that leads only with `Green 완료 / Yellow 대기 / Red 필요` is also operationally incomplete for BO-099-style work. It may contain the ledger, but it does not answer the repo-topology question Chris actually asked: product work should land on `develop`, release should flow from `develop` to `main`, and runtime/tooling work should queue live application separately.

For runtime/tooling repos, a closeout missing restart/live/queue fields is incomplete when code/config changes could affect live runtime behavior. Restart-needed items go to Yellow until Chris explicitly says `대기열 적용해` or `restart 필요한 것 모아서 적용해`.
