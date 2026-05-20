// Bug Fix: Pre-live: flatten_orphans.py --execute bypasses risk_circuit_state
//
// Requirements:
// 1. [ ] Before placing any order, \`flatten_orphans.py --execute\` reads \`risk_circuit_state\` (use \`repo.load_risk_state\` like \`RiskManager._hydrate_persisted_state\` does — see \`trading_bot/execution/risk_manager.py:130-131\`).
// 2. [ ] If \`is_paused\`, \`daily_loss_limit_hit\`, \`drawdown_breaker_active\`, or \`commission_stop_active\` is set, refuse the flatten unless \`--force-during-halt\` is **also** supplied.
// 3. [ ] \`--force-during-halt\` without \`--execute\` is a no-op (warn + exit).
// 4. [ ] The halt-state read happens **before** any broker call so the refusal path doesn't even open the Alpaca connection.
// 5. [ ] Tests cover: (a) no halt + --execute → submits; (b) halt + --execute → refuses with exit code 2; (c) halt + --execute + --force-during-halt → submits with WARNING log; (d) dry-run is unaffected by halt state.
// 6. [ ] \`README.md\` / wherever flatten_orphans is documented gets the new flag mentioned.
//
// Approach:
1. 🔍 Comprehensive Requirement Analysis
   - Core Objective: ## Problem

[trading_bot/self_improve/flatten_orph...
2. 🏗️ Structural Scaffolding & Component Design
3. 💻 Core Logic Implementation Phase
   - Address key requirement: [ ] Before placing any order, \`flatten_orphans.py --execute...
   - Address key requirement: [ ] If \`is_paused\`, \`daily_loss_limit_hit\`, \`drawdown_b...
   - Address key requirement: [ ] \`--force-during-halt\` without \`--execute\` is a no-op...
4. 🧪 Rigorous QA & Verification Loop
   - Verify all acceptance criteria against implementation
   - Perform edge-case stress tests and regression checks
5. 🚀 Final Polish & Submission
   - Code optimization, documentation, and PR creation

// Implementation
export function fix() {
  // TODO: Implement fix based on requirements
}

export default fix;
