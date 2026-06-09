/* ============================================================
   NERVE — mission data (v2)
   Each mission = a graph + an ordered timeline of "beats".
   A beat can: light a node, stream an event, set risk, move a
   KPI (mv), and write/contradict shared memory (mem).
   ============================================================ */

const AGENTS = [
  { id: 'planner',   name: 'Planner',   role: 'plans',   color: '#9d8bff' },
  { id: 'execution', name: 'Execution', role: 'acts',    color: '#3fe0c5' },
  { id: 'risk',      name: 'Risk',      role: 'scores',  color: '#f4c25a' },
  { id: 'auditor',   name: 'Auditor',   role: 'checks',  color: '#46d39a' },
];
const ORCH = { id: 'orchestrator', name: 'Orchestrator', color: '#dcebe8' };

const AGENT_COLOR = {
  planner: '#9d8bff', execution: '#3fe0c5', risk: '#f4c25a',
  auditor: '#46d39a', orchestrator: '#dcebe8', system: '#5d6e6c',
};

const ev = (kind, agent, text, tool) => ({ kind, agent, text, tool });
/* memory op: { k, label, v, conf, op:'write'|'update'|'confirm'|'contradict' } */

/* ============================================================
   MISSION 1 — INCIDENT AUTOPILOT
   ============================================================ */
const INCIDENT = {
  id: 'incident',
  label: 'Incident Autopilot',
  goal: 'Keep <span class="hl">checkout</span> healthy — detect, diagnose & resolve production anomalies.',
  goalText: 'Keep checkout healthy — detect, diagnose and resolve production incidents automatically.',
  integrations: ['Dynatrace', 'GitLab', 'PagerDuty'],
  metric: { label: 'checkout · p99', unit: 'ms', baseline: 410, lowerBetter: true },
  nodes: [
    { id: 'detect',    x: 9,  y: 50, agent: 'execution', icon: 'pulse',  label: 'Detect anomaly' },
    { id: 'correlate', x: 30, y: 28, agent: 'execution', icon: 'git',    label: 'Correlate recent deploys' },
    { id: 'impact',    x: 30, y: 72, agent: 'risk',      icon: 'radius', label: 'Assess blast radius' },
    { id: 'diagnose',  x: 50, y: 50, agent: 'planner',   icon: 'target', label: 'Root-cause analysis' },
    { id: 'propose',   x: 71, y: 30, agent: 'planner',   icon: 'route',  label: 'Propose rollback' },
    { id: 'approve',   x: 71, y: 72, agent: 'orchestrator', icon: 'shield', label: 'Human approval', kind: 'approval' },
    { id: 'execute',   x: 91, y: 50, agent: 'execution', icon: 'bolt',   label: 'Execute rollback' },
    { id: 'verify',    x: 91, y: 78, agent: 'auditor',   icon: 'check',  label: 'Verify recovery' },
    { id: 'conflict',  x: 50, y: 9,  agent: 'risk',      icon: 'alert',  label: 'Conflicting deploy #4830', chaos: true },
  ],
  edges: [
    ['detect','correlate'], ['detect','impact'],
    ['correlate','diagnose'], ['impact','diagnose'],
    ['diagnose','propose'], ['propose','approve'],
    ['approve','execute'], ['execute','verify'],
  ],
  chaosEdges: [ ['conflict','diagnose'] ],
  approvalNode: 'approve',

  beats: [
    { node: ['detect','active'], phase: 'Detecting', risk: 34, agent: 'execution', dur: 2200, mv: 4200,
      mem: [{ k: 'anomaly', label: 'Anomaly', v: 'checkout p99 4.2s', conf: 0.94, op: 'write' }],
      event: ev('alert','execution','<span class="bad em">Anomaly</span> on <span class="em">checkout-svc</span> — p99 latency <span class="num">4.2s</span> (baseline <span class="num">410ms</span>), error rate <span class="bad">12%</span>.','Dynatrace · Davis AI') },
    { node: ['detect','done'], phase: 'Detecting', risk: 38, agent: 'risk', dur: 1500, mv: 4200,
      event: ev('risk','risk','Confidence this is real, not noise: <span class="num">0.94</span>. Opening mission.') },

    { node: ['correlate','active'], phase: 'Correlating', risk: 44, agent: 'execution', dur: 2400, mv: 4200,
      event: ev('signal','execution','Pulling deploys to <span class="em">checkout-svc</span> in last 30m…','GitLab · CI/CD') },
    { node: ['correlate','done'], phase: 'Correlating', risk: 46, agent: 'execution', dur: 900, mv: 4200,
      mem: [{ k: 'suspect', label: 'Root cause', v: 'deploy #4827', conf: 0.62, op: 'write' }],
      event: ev('signal','execution','<span class="num">2</span> deploys found. <span class="em">#4827</span> shipped <span class="num">6m</span> before the spike.') },

    { node: ['impact','active'], phase: 'Correlating', risk: 56, agent: 'risk', dur: 2100, mv: 4300,
      mem: [{ k: 'blast', label: 'Blast radius', v: '38% of traffic', conf: 0.9, op: 'write' }],
      event: ev('risk','risk','Blast radius: <span class="bad">38%</span> of checkout traffic affected, ~<span class="num">2,400</span> sessions/min failing.') },
    { node: ['impact','done'], phase: 'Correlating', risk: 54, agent: 'auditor', dur: 1200, mv: 4300,
      event: ev('memory','auditor','State consistent — anomaly, deploy window & impact all agree.') },

    { node: ['diagnose','active'], phase: 'Diagnosing', risk: 50, agent: 'planner', dur: 2600, mv: 4150,
      mem: [{ k: 'suspect', label: 'Root cause', v: '#4827 · conn-pool', conf: 0.88, op: 'update' }],
      event: ev('signal','planner','Tracing failure to <span class="em">#4827</span> — new DB connection pool, exhausting under load. Primary suspect.') },
    { node: ['diagnose','done'], phase: 'Diagnosing', risk: 46, agent: 'planner', dur: 1100, mv: 4100,
      event: ev('memory','planner','Root cause locked. Writing remediation plan to shared memory.') },

    { node: ['propose','active'], phase: 'Planning', risk: 44, agent: 'planner', dur: 2300, mv: 4050,
      mem: [{ k: 'plan', label: 'Plan', v: 'rollback → #4826', conf: 0.82, op: 'write' }],
      event: ev('signal','planner','Plan: roll <span class="em">checkout-svc</span> back to <span class="em">#4826</span> (last-known-good, 41m ago). Est. recovery <span class="num">90s</span>.') },
    { node: ['propose','done'], phase: 'Planning', risk: 48, agent: 'risk', dur: 1000, mv: 4000,
      event: ev('risk','risk','Rollback is <span class="em">consequential</span> — touches production. Routing to human.') },

    { node: ['approve','approval'], phase: 'Awaiting approval', risk: 58, agent: 'orchestrator', dur: 0, mv: 3950,
      approval: {
        title: 'Roll back checkout-svc to #4826',
        detail: 'NERVE wants to <span class="em">revert deploy #4827</span> to stop the latency spike. This runs a GitLab rollback pipeline on production.',
        rows: [
          ['Target', 'checkout-svc · prod'],
          ['Action', 'Rollback #4827 → #4826'],
          ['Predicted recovery', '~90s'],
          ['If wrong', 'Re-deploy #4827, +2m'],
        ],
        approve: 'Approve rollback', reject: 'Hold', integration: 'GitLab pipeline',
        impact: { kind: 'pods', total: 12, hit: 12, label: 'pods · checkout-svc', sub: 'rolling restart · no downtime' },
      },
      event: ev('approval','orchestrator','<span class="em">Approval required</span> — rollback of <span class="em">#4827</span>. Mirrored to Telegram. Holding.') },

    { node: ['execute','active'], phase: 'Executing', risk: 50, agent: 'execution', dur: 2600, afterApproval: true, mv: 3850,
      event: ev('signal','execution','Approved. Triggering rollback pipeline <span class="em">#pipe-9920</span>…','GitLab · pipeline') },
    { node: ['execute','done'], phase: 'Executing', risk: 40, agent: 'execution', dur: 1200, mv: 2300,
      event: ev('success','execution','Rollback deployed. <span class="em">#4826</span> live on all <span class="num">12</span> pods.') },

    { node: ['verify','active'], phase: 'Verifying', risk: 26, agent: 'auditor', dur: 2400, mv: 1050,
      event: ev('signal','auditor','Watching metrics… p99 <span class="num">4.2s → 1.1s → 0.4s</span>, error rate <span class="good">12% → 0.3%</span>.','Dynatrace') },
    { node: ['verify','done'], phase: 'Resolved', risk: 8, agent: 'auditor', dur: 900, mv: 380,
      mem: [{ k: 'status', label: 'Status', v: 'recovered', conf: 0.97, op: 'write' }],
      event: ev('success','auditor','<span class="good em">Recovered.</span> Metrics within baseline for <span class="num">60s</span>.') },
    { node: null, phase: 'Resolved', risk: 6, agent: 'orchestrator', dur: 0, done: true, mv: 380,
      event: ev('memory','orchestrator','Incident closed. Postmortem drafted & filed. Mission complete.') },
  ],

  chaos: [
    { node: ['conflict','alert'], phase: 'Re-planning', risk: 82, agent: 'risk', dur: 2200, born: 'conflict', mv: 4600,
      mem: [{ k: 'suspect', label: 'Root cause', v: '#4827 vs #4830 ?', conf: 0.41, op: 'contradict' }],
      event: ev('alert','risk','<span class="bad em">Conflict.</span> Dynatrace now flags <span class="em">#4830</span> (payments-svc) deployed <span class="num">2m</span> ago — second suspect.') },
    { node: ['diagnose','active'], phase: 'Re-planning', risk: 78, agent: 'risk', dur: 1400, mv: 4550,
      event: ev('risk','risk','Risk <span class="bad">0.82</span> — too high to act. <span class="em">Tripping re-plan.</span>') },
    { node: null, phase: 'Re-planning', risk: 70, agent: 'execution', dur: 2400, mv: 4500,
      event: ev('signal','execution','Re-correlating traces across both services to disambiguate…','Dynatrace · traces') },
    { node: ['conflict','done'], phase: 'Re-planning', risk: 50, agent: 'planner', dur: 2000, mv: 4400,
      mem: [{ k: 'suspect', label: 'Root cause', v: 'deploy #4827', conf: 0.9, op: 'confirm' }],
      event: ev('memory','planner','<span class="em">#4830</span> ruled out — its errors are downstream <em>of</em> checkout, not upstream. <span class="em">#4827</span> remains root cause.') },
    { node: ['diagnose','done'], phase: 'Diagnosing', risk: 46, agent: 'auditor', dur: 1000, mv: 4100,
      event: ev('memory','auditor','State reconciled. Resuming original plan with higher confidence.') },
  ],

  /* user pressed "Hold" instead of approving — NERVE returns with a safer option */
  rejectFollow: [
    { node: ['approve','pending'], phase: 'Re-planning', risk: 52, agent: 'planner', dur: 2000, mv: 4000,
      event: ev('memory','planner','Held. Searching for a lower-risk fix than a full deploy rollback…') },
    { node: ['propose','active'], phase: 'Re-planning', risk: 48, agent: 'planner', dur: 2300, mv: 4000,
      mem: [{ k: 'plan', label: 'Plan', v: 'revert config only', conf: 0.8, op: 'update' }],
      event: ev('signal','planner','Alternative: revert only the <span class="em">connection-pool config</span> on #4827 — reversible in <span class="num">5s</span>, no redeploy.') },
    { node: ['propose','done'], phase: 'Re-planning', risk: 46, agent: 'risk', dur: 1000, mv: 4000,
      event: ev('risk','risk','Lower blast radius. Still consequential — routing the alternative for approval.') },
    { node: ['approve','approval'], phase: 'Awaiting approval', risk: 50, agent: 'orchestrator', dur: 0, mv: 4000,
      approval: {
        title: 'Revert connection-pool config',
        detail: 'Safer option — revert only the <span class="em">config flag</span> from #4827, not the whole deploy.',
        rows: [
          ['Target', 'checkout-svc · prod'],
          ['Action', 'Config revert (no redeploy)'],
          ['Predicted recovery', '~30s'],
          ['If wrong', 'Re-apply, +10s'],
        ],
        approve: 'Approve config revert', reject: 'Hold', integration: 'GitLab',
        impact: { kind: 'pods', total: 12, hit: 12, label: 'pods · live config reload', sub: 'rolling · no restart' },
      },
      event: ev('approval','orchestrator','<span class="em">Alternative approval</span> — config-only revert. Mirrored to Telegram.') },
    { node: ['execute','active'], phase: 'Executing', risk: 42, agent: 'execution', dur: 2400, afterApproval: true, mv: 3700,
      event: ev('signal','execution','Approved. Reverting config flag on all <span class="num">12</span> pods…','GitLab · config') },
    { node: ['execute','done'], phase: 'Executing', risk: 32, agent: 'execution', dur: 1200, mv: 1600,
      event: ev('success','execution','Config reverted. Connection pools recycled cleanly.') },
    { node: ['verify','active'], phase: 'Verifying', risk: 22, agent: 'auditor', dur: 2300, mv: 900,
      event: ev('signal','auditor','Metrics recovering: p99 <span class="num">4.2s → 0.9s → 0.4s</span>.','Dynatrace') },
    { node: ['verify','done'], phase: 'Resolved', risk: 9, agent: 'auditor', dur: 900, mv: 380,
      mem: [{ k: 'status', label: 'Status', v: 'recovered (safe path)', conf: 0.96, op: 'write' }],
      event: ev('success','auditor','<span class="good em">Recovered</span> via the safer path — within baseline.') },
    { node: null, phase: 'Resolved', risk: 7, agent: 'orchestrator', dur: 0, done: true, mv: 380,
      event: ev('memory','orchestrator','Incident closed using the human-preferred mitigation. Postmortem filed.') },
  ],
};

/* ============================================================
   MISSION 2 — RESEARCH AGENT
   ============================================================ */
const RESEARCH = {
  id: 'research',
  label: 'Research Agent',
  goal: 'Find the cheapest <span class="hl">2026 World Cup final</span> ticket + a cheap, well-reviewed hotel nearby.',
  goalText: 'Find the cheapest 2026 World Cup final ticket and a cheap, well-reviewed hotel nearby.',
  integrations: ['Web Search', 'Browser', 'Maps'],
  metric: { label: 'cheapest ticket', unit: '$', baseline: null, lowerBetter: true, prefix: '$' },
  nodes: [
    { id: 'decompose', x: 9,  y: 50, agent: 'planner',   icon: 'route',  label: 'Decompose goal' },
    { id: 'tickets',   x: 30, y: 28, agent: 'execution', icon: 'ticket', label: 'Search ticket resellers' },
    { id: 'hotels',    x: 30, y: 72, agent: 'execution', icon: 'bed',    label: 'Search hotels < 2km' },
    { id: 'cmp_t',     x: 50, y: 28, agent: 'execution', icon: 'scale',  label: 'Score 14 listings' },
    { id: 'cmp_h',     x: 50, y: 72, agent: 'risk',      icon: 'star',   label: 'Filter by rating ≥ 4.3' },
    { id: 'rank',      x: 70, y: 50, agent: 'planner',   icon: 'target', label: 'Cross-rank price × quality' },
    { id: 'approve',   x: 90, y: 30, agent: 'orchestrator', icon: 'shield', label: 'Human approval', kind: 'approval' },
    { id: 'deliver',   x: 90, y: 72, agent: 'auditor',   icon: 'check',  label: 'Deliver recommendation' },
    { id: 'soldout',   x: 50, y: 9,  agent: 'risk',      icon: 'alert',  label: 'Top ticket sold out', chaos: true },
  ],
  edges: [
    ['decompose','tickets'], ['decompose','hotels'],
    ['tickets','cmp_t'], ['hotels','cmp_h'],
    ['cmp_t','rank'], ['cmp_h','rank'],
    ['rank','approve'], ['approve','deliver'],
  ],
  chaosEdges: [ ['soldout','rank'] ],
  approvalNode: 'approve',

  beats: [
    { node: ['decompose','active'], phase: 'Planning', risk: 16, agent: 'planner', dur: 2200,
      event: ev('signal','planner','Goal decomposed → <span class="num">3</span> subtasks: <span class="em">find tickets</span>, <span class="em">find hotels</span>, <span class="em">cross-rank</span>.') },
    { node: ['decompose','done'], phase: 'Planning', risk: 18, agent: 'planner', dur: 900,
      mem: [{ k: 'cons', label: 'Constraints', v: 'final · ≤2km · ≥4.3★', conf: 1, op: 'write' }],
      event: ev('memory','planner','Constraints stored: final · New York/NJ · ticket = cheapest, hotel ≤ 2km & rating ≥ 4.3.') },

    { node: ['tickets','active'], phase: 'Searching', risk: 22, agent: 'execution', dur: 2300,
      event: ev('signal','execution','Querying resale markets for <span class="em">Final · MetLife Stadium</span>…','Web Search') },
    { node: ['tickets','done'], phase: 'Searching', risk: 24, agent: 'execution', dur: 800, mv: 1180,
      mem: [{ k: 'tkt', label: 'Cheapest ticket', v: '$1,180 · Cat 3', conf: 0.9, op: 'write' }],
      event: ev('signal','execution','<span class="num">14</span> listings found across <span class="num">4</span> resellers. Cheapest <span class="num">$1,180</span> (Cat 3).') },

    { node: ['hotels','active'], phase: 'Searching', risk: 24, agent: 'execution', dur: 2200, mv: 1180,
      event: ev('signal','execution','Searching hotels within <span class="num">2km</span> of MetLife Stadium…','Maps · Browser') },
    { node: ['hotels','done'], phase: 'Searching', risk: 26, agent: 'execution', dur: 800, mv: 1180,
      event: ev('signal','execution','<span class="num">31</span> hotels in radius. Caching prices for finals weekend.') },

    { node: ['cmp_t','active'], phase: 'Scoring', risk: 28, agent: 'execution', dur: 2000, mv: 1180,
      event: ev('signal','execution','Scoring listings on price, seat view & seller trust…') },
    { node: ['cmp_t','done'], phase: 'Scoring', risk: 26, agent: 'auditor', dur: 900, mv: 1180,
      event: ev('memory','auditor','Dropped <span class="num">3</span> listings — seller rating < 90%. Kept <span class="num">11</span>.') },

    { node: ['cmp_h','active'], phase: 'Scoring', risk: 28, agent: 'risk', dur: 2000, mv: 1180,
      event: ev('risk','risk','Filtering hotels: rating ≥ <span class="num">4.3</span>, walkable, free cancellation.') },
    { node: ['cmp_h','done'], phase: 'Scoring', risk: 25, agent: 'risk', dur: 900, mv: 1180,
      mem: [{ k: 'htl', label: 'Best hotel', v: 'Rutherford 4.6★ · $226', conf: 0.88, op: 'write' }],
      event: ev('signal','risk','<span class="num">6</span> hotels pass. Best value: <span class="em">The Rutherford</span> — 4.6★, <span class="num">$226</span>/night, 1.3km.') },

    { node: ['rank','active'], phase: 'Ranking', risk: 30, agent: 'planner', dur: 2400, mv: 1180,
      event: ev('signal','planner','Cross-ranking bundles on total cost × quality × convenience…') },
    { node: ['rank','done'], phase: 'Ranking', risk: 28, agent: 'planner', dur: 1000, mv: 1180,
      mem: [{ k: 'bnd', label: 'Best bundle', v: '$1,632 total', conf: 0.85, op: 'write' }],
      event: ev('memory','planner','Top bundle: ticket <span class="num">$1,180</span> + hotel <span class="num">$452</span> (2 nights) = <span class="num">$1,632</span>.') },

    { node: ['approve','approval'], phase: 'Awaiting approval', risk: 40, agent: 'orchestrator', dur: 0, mv: 1180,
      approval: {
        title: 'Reserve the top hotel bundle',
        detail: 'NERVE wants to <span class="em">place a 24h hold</span> on the best hotel and lock the listed ticket price. This holds <span class="num">$452</span> on your card.',
        rows: [
          ['Hotel', 'The Rutherford · 4.6★'],
          ['Nights', '2 · 1.3km from venue'],
          ['Ticket', 'Cat 3 · $1,180 locked'],
          ['Total hold', '$452 refundable'],
        ],
        approve: 'Approve & reserve', reject: 'Just show me', integration: 'Browser · checkout',
        impact: { kind: 'cost', amount: '$452', label: 'funds on hold', sub: 'refundable · 24h window' },
      },
      event: ev('approval','orchestrator','<span class="em">Approval required</span> — reserving holds funds. Mirrored to Telegram. Holding.') },

    { node: ['approve','done'], phase: 'Delivering', risk: 22, agent: 'execution', dur: 2200, afterApproval: true, mv: 1180,
      event: ev('signal','execution','Approved. Placing 24h hold & locking ticket price…','Browser · checkout') },
    { node: ['deliver','active'], phase: 'Delivering', risk: 16, agent: 'auditor', dur: 2000, mv: 1180,
      event: ev('signal','auditor','Compiling ranked recommendation with direct booking links.') },
    { node: ['deliver','done'], phase: 'Delivered', risk: 10, agent: 'auditor', dur: 900, mv: 1180,
      mem: [{ k: 'st', label: 'Status', v: 'delivered · hold #H-2261', conf: 0.95, op: 'write' }],
      event: ev('success','auditor','<span class="good em">Done.</span> Hold confirmed · ref <span class="em">#H-2261</span>. Bundle saves <span class="good">$310</span> vs. average.') },
    { node: null, phase: 'Delivered', risk: 8, agent: 'orchestrator', dur: 0, done: true, mv: 1180,
      event: ev('memory','orchestrator','Recommendation delivered with 3 ranked options & links to act. Mission complete.') },
  ],

  chaos: [
    { node: ['soldout','alert'], phase: 'Re-planning', risk: 70, agent: 'risk', dur: 2200, born: 'soldout', mv: 1650,
      mem: [{ k: 'tkt', label: 'Cheapest ticket', v: '$1,180 SOLD OUT', conf: 0.2, op: 'contradict' }],
      event: ev('alert','risk','<span class="bad em">Listing gone.</span> Cheapest ticket (<span class="num">$1,180</span>) sold out mid-search. Re-pricing surged <span class="bad">+40%</span>.') },
    { node: ['rank','active'], phase: 'Re-planning', risk: 64, agent: 'risk', dur: 1400, mv: 1650,
      event: ev('risk','risk','Top bundle invalid. <span class="em">Tripping re-plan</span> before recommending a stale price.') },
    { node: null, phase: 'Re-planning', risk: 55, agent: 'execution', dur: 2300, mv: 1500,
      event: ev('signal','execution','Re-querying live inventory across all resellers…','Web Search') },
    { node: ['soldout','done'], phase: 'Re-planning', risk: 38, agent: 'planner', dur: 2000, mv: 1240,
      mem: [{ k: 'tkt', label: 'Cheapest ticket', v: '$1,240 · Cat 3 (live)', conf: 0.86, op: 'update' }],
      event: ev('memory','planner','Found <span class="em">$1,240</span> Cat 3 (verified). Re-ranking — still cheapest valid option.') },
    { node: ['rank','done'], phase: 'Ranking', risk: 28, agent: 'auditor', dur: 1000, mv: 1240,
      mem: [{ k: 'bnd', label: 'Best bundle', v: '$1,692 total', conf: 0.84, op: 'update' }],
      event: ev('memory','auditor','Plan refreshed with live prices. Confidence restored. Resuming.') },
  ],

  /* user pressed "Just show me" — deliver the list without booking */
  rejectFollow: [
    { node: ['approve','done'], phase: 'Delivering', risk: 18, agent: 'auditor', dur: 2000, mv: 1180,
      event: ev('memory','auditor','No reservation — got it. Nothing booked. Compiling the ranked list to hand off.') },
    { node: ['deliver','active'], phase: 'Delivering', risk: 14, agent: 'auditor', dur: 2000, mv: 1180,
      event: ev('signal','auditor','Building recommendation with direct booking links so you can act when ready.') },
    { node: ['deliver','done'], phase: 'Delivered', risk: 10, agent: 'auditor', dur: 900, mv: 1180,
      mem: [{ k: 'st', label: 'Status', v: 'delivered · not booked', conf: 0.94, op: 'write' }],
      event: ev('success','auditor','<span class="good em">Delivered.</span> 3 ranked bundles, cheapest <span class="num">$1,632</span>. You book on your terms.') },
    { node: null, phase: 'Delivered', risk: 8, agent: 'orchestrator', dur: 0, done: true, mv: 1180,
      event: ev('memory','orchestrator','Recommendation handed off. No funds held. Mission complete.') },
  ],
};

const MISSIONS = { incident: INCIDENT, research: RESEARCH };

/* implied fleet — makes NERVE feel like it runs many missions at once */
const FLEET_GHOSTS = [
  { id: 'cost-audit', label: 'Nightly cost audit', status: 'queued', sub: 'starts 02:00' },
  { id: 'sec-sweep',  label: 'Dependency CVE sweep', status: 'running', sub: 'no action needed' },
];

window.NERVE = { AGENTS, ORCH, AGENT_COLOR, MISSIONS, INCIDENT, RESEARCH, FLEET_GHOSTS };
