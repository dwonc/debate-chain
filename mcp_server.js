#!/usr/bin/env node
/**
 * Horcrux MCP Server v8.0
 * Adaptive 단일 진입점 — 도구 5개
 *
 * - run: 통합 실행 (자연어 task → 자동 분류 → 최적 엔진)
 * - check: job 상태 확인 + 완료 시 결과 포함
 * - classify: task 분류 미리보기
 * - analytics: 운영 데이터 대시보드
 * - horcrux_test: AI 연결 테스트
 */
const { McpServer } = require("@modelcontextprotocol/sdk/server/mcp.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const { z } = require("zod");
const http = require("http");

const FLASK = "http://localhost:5000";

function flask(path, method, body) {
  return new Promise((res, rej) => {
    const u = new URL(path, FLASK);
    const r = http.request({
      hostname: u.hostname, port: u.port, path: u.pathname + u.search,
      method: method || "GET",
      headers: { "Content-Type": "application/json", "X-API-Key": process.env.HORCRUX_API_KEY || "" },
      timeout: 600000
    }, (resp) => {
      let d = "";
      resp.on("data", c => d += c);
      resp.on("end", () => { try { res(JSON.parse(d)); } catch { res({ raw: d }); } });
    });
    r.on("error", rej);
    r.on("timeout", () => { r.destroy(); rej(new Error("HTTP timeout")); });
    if (body) r.write(JSON.stringify(body));
    r.end();
  });
}

const server = new McpServer({ name: "horcrux", version: "8.0" });

// ─── 1. run: 통합 실행 ───
server.tool("run",
  "Horcrux 메인 실행. task만 넣으면 자동으로 최적 모드/엔진을 선택. 코드 수정, 브레인스토밍, 문서 작성, PPT 생성, 아키텍처 설계 등 모든 작업을 이 도구 하나로 처리. 수동 모드 지정도 가능(fast/standard/full/parallel).",
  {
    task: z.string().describe("자연어 task 설명"),
    mode: z.enum(["auto", "fast", "standard", "full", "parallel"]).default("auto").describe("auto=자동 분류, fast/standard/full=수동 override, parallel=병렬 생성"),
    scope: z.enum(["small", "medium", "large"]).optional().describe("작업 규모 (auto-detected if omitted)"),
    risk: z.enum(["low", "medium", "high"]).optional().describe("위험도 (auto-detected if omitted)"),
    artifact_type: z.enum(["none", "ppt", "pdf", "doc"]).optional().describe("산출물 형식"),
    output_dir: z.string().optional().describe("parallel mode only: 파일 자동 저장 경로"),
    project_dir: z.string().optional().describe("프로젝트 코드를 context로 활용"),
    audience: z.string().optional().describe("문서/PPT 타겟 독자"),
    tone: z.string().optional().describe("professional/casual/technical"),
    iterations: z.number().optional().describe("self_improve 반복 횟수"),
    interactive: z.enum(["batch", "interactive", "semi"]).optional().describe("batch=기본, interactive=매 라운드 pause, semi=조건부 pause"),
  },
  async (args) => {
    try {
      const body = { task: args.task, mode: args.mode || "auto" };
      if (args.scope) body.scope = args.scope;
      if (args.risk) body.risk = args.risk;
      if (args.artifact_type) body.artifact_type = args.artifact_type;
      if (args.output_dir) body.output_dir = args.output_dir;
      if (args.project_dir) body.project_dir = args.project_dir;
      if (args.audience) body.audience = args.audience;
      if (args.interactive) body.interactive = args.interactive;
      if (args.tone) body.tone = args.tone;
      if (args.iterations) body.iterations = args.iterations;

      // parallel mode: pair_mode 매핑
      if (args.mode === "parallel") {
        body.pair_mode = "pair2";  // 기본 2-AI, task에 "3" 포함 시 pair3
        if (/3|three|세|셋/.test(args.task)) body.pair_mode = "pair3";
      }

      const r = await flask("/api/horcrux/run", "POST", body);
      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };

      // 동기 응답 (adaptive_* 엔진)
      if (r.solution) {
        let t = `## Horcrux: ${r.status === "converged" ? "CONVERGED ✅" : "COMPLETED"}\n\n`;
        t += `**Mode:** ${r.mode} | **Engine:** ${r.internal_engine} | **Score:** ${r.score || 0}/10 | **Rounds:** ${r.rounds || 0}\n`;
        t += `**Routing:** ${r.routing?.source} (confidence: ${((r.routing?.confidence || 0) * 100).toFixed(0)}%, intent: ${r.routing?.intent})\n`;
        t += `\n## Solution\n\`\`\`\n${(r.solution || "").slice(0, 6000)}\n\`\`\``;
        if ((r.solution || "").length > 6000) t += `\n\n(truncated, full: ${r.solution.length} chars)`;
        return { content: [{ type: "text", text: t }] };
      }

      // 비동기 응답 (planning/pair/debate/self_improve)
      let t = `## Job Started\n\n`;
      t += `**Engine:** ${r.internal_engine} | **Mode:** ${r.mode}\n`;
      t += `**Job ID:** ${r.job_id}\n`;
      t += `**Routing:** ${r.routing?.source} (confidence: ${((r.routing?.confidence || 0) * 100).toFixed(0)}%, intent: ${r.routing?.intent})\n\n`;
      t += `Use \`check("${r.job_id}")\` to monitor progress.`;
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

// ─── 2. check: 통합 상태 확인 + 완료 시 자동 결과 ───
server.tool("check",
  "실행 중인 job 상태 확인. 완료 시 자동으로 결과 포함. debate/pair/planning/self_improve 모든 job_id 대응.",
  { job_id: z.string() },
  async (args) => {
    try {
      const id = args.job_id;

      // pipeline (dp_)
      if (id.startsWith("dp_")) {
        const status = await flask(`/api/pipeline/status/${id}`);
        if (status.error) return { content: [{ type: "text", text: "Not found: " + id }] };
        if (status.status === "running") {
          return { content: [{ type: "text", text: `pipeline: ${status.status} | phase: ${status.phase} | debate: ${status.debate_id || "pending"} | pair: ${status.pair_id || "pending"}` }] };
        }
        const full = await flask(`/api/pipeline/result/${id}`);
        let t = `## Pipeline ${status.status}\n`;
        if (full.debate) {
          t += `\nDebate: ${full.debate.status} (score: ${full.debate.avg_score}/10, ${full.debate.round} rounds)\n`;
          if (full.debate.final_solution) t += `\nFinal design:\n${full.debate.final_solution.slice(0, 2000)}`;
        }
        if (full.pair) {
          t += `\n\nPair (${full.pair.mode}): ${full.pair.status}\n`;
          for (const msg of (full.pair.messages || [])) {
            if (msg.role === "architect") continue;
            t += `\n### ${msg.role} (${msg.model || "?"})\n${(msg.content || "").slice(0, 3000)}\n`;
          }
        }
        if (t.length > 12000) t = t.slice(0, 12000) + "\n\n[...truncated]";
        return { content: [{ type: "text", text: t }] };
      }

      // pair (pair_)
      if (id.startsWith("pair_")) {
        const status = await flask(`/api/pair/status/${id}`);
        if (!status.id && status.error) return { content: [{ type: "text", text: "Not found: " + id }] };
        if (status.status === "running") {
          return { content: [{ type: "text", text: `pair: ${status.status} | phase: ${status.phase} | parts_done: ${status.parts_done || 0}` }] };
        }
        if (status.status === "error") {
          return { content: [{ type: "text", text: `pair: ERROR\n${status.error || "unknown error"}\n\nphase: ${status.phase || "-"}` }] };
        }
        const full = await flask(`/api/pair/result/${id}`);
        let t = `## Pair ${(full.mode || "").toUpperCase()} ${full.status}\n\n`;
        t += `### Spec\n${full.spec || "N/A"}\n\n`;
        for (const msg of (full.messages || [])) {
          if (msg.role === "architect") continue;
          t += `### ${msg.role} (${msg.model || "?"})\n${(msg.content || "").slice(0, 3000)}\n\n`;
        }
        if (t.length > 12000) t = t.slice(0, 12000) + "\n\n[...truncated]";
        return { content: [{ type: "text", text: t }] };
      }

      // planning (plan_)
      if (id.startsWith("plan_")) {
        const status = await flask(`/api/planning/status/${id}`);
        if (status.error) return { content: [{ type: "text", text: "Not found: " + id }] };
        if (status.status === "running") {
          return { content: [{ type: "text", text: `planning: ${status.status} | phase: ${status.phase} | ${status.phase_detail || "..."}\nmessages: ${status.message_count || 0} | avg_score: ${(status.avg_score || 0).toFixed(1)}/10` }] };
        }
        const full = await flask(`/api/planning/result/${id}`);
        let t = `## Planning ${full.status} (Avg Critic: ${(full.avg_score || 0).toFixed(1)}/10)\n\n`;
        const msgs = full.messages || [];
        const generators = msgs.filter(m => m.role === "generator");
        const critics = msgs.filter(m => m.role === "critic");
        if (generators.length) {
          t += `### Phase 1: ${generators.length} independent plans generated\n`;
          for (const g of generators) t += `- ${g.label || g.model || "?"}\n`;
          t += "\n";
        }
        const synth = msgs.find(m => m.role === "synthesizer");
        if (synth) t += `### Phase 2: Opus synthesized unified plan\n${(synth.content || "").slice(0, 2000)}\n\n`;
        if (critics.length) {
          t += `### Phase 3: ${critics.length} critics reviewed\n`;
          for (const c of critics) t += `- ${c.label || c.model}: ${(c.score || "?")}/10\n`;
          t += "\n";
        }
        const final_ = msgs.find(m => m.role === "final");
        if (final_) t += `### Phase 4: Final Plan (Codex)\n${(final_.content || "").slice(0, 4000)}\n`;
        if (t.length > 12000) t = t.slice(0, 12000) + "\n\n[...truncated]";
        return { content: [{ type: "text", text: t }] };
      }

      // self_improve (si_)
      if (id.startsWith("si_")) {
        const status = await flask(`/api/self_improve/status/${id}`);
        if (status.error) return { content: [{ type: "text", text: "Not found: " + id }] };
        if (status.status === "running") {
          return { content: [{ type: "text", text: `self_improve: iteration ${status.iteration}/${status.total_iterations}` }] };
        }
        const full = await flask(`/api/self_improve/result/${id}`);
        let t = `## Self-Improve ${full.status} (score: ${full.final_score}/10)\n\n${full.final_solution || ""}`;
        if (t.length > 12000) t = t.slice(0, 12000) + "\n\n[...truncated]";
        return { content: [{ type: "text", text: t }] };
      }

      // debate (default)
      const status = await flask(`/api/status/${id}`);
      if (status.error) return { content: [{ type: "text", text: "Not found: " + id }] };
      if (status.status === "running") {
        return { content: [{ type: "text", text: `debate: Round ${status.round} | Score: ${(status.avg_score || 0).toFixed(1)}/10 | phase: ${status.phase || "..."}` }] };
      }
      const full = await flask(`/api/result/${id}`);
      let t = `## ${full.status === "converged" ? "✅ Converged" : "⚠️ " + full.status}\n`;
      t += `Rounds: ${full.round} | Score: ${(full.avg_score || 0).toFixed(1)}/10\n\n`;
      const msgs = full.messages || [];
      const lastGenIdx = msgs.reduce((li, m, i) => m.role === "generator" ? i : li, 0);
      const lastMsgs = msgs.slice(lastGenIdx);
      for (const m of lastMsgs) {
        if (m.role === "generator") t += `### Round ${full.round} Generator\n${(m.content || "").slice(0, 2000)}\n\n`;
        else if (m.role === "critic") t += `### Critic (${(m.score || 0).toFixed(1)}/10)\n${(m.content || "").slice(0, 1000)}\n\n`;
        else if (m.role === "synthesizer") t += `### Synthesizer\n${(m.content || "").slice(0, 2000)}\n\n`;
      }
      if (full.final_solution) t += `## Final Solution\n${full.final_solution.slice(0, 4000)}`;
      if (t.length > 12000) t = t.slice(0, 12000) + "\n\n[...truncated]";
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

// ─── 3. classify: 분류 미리보기 ───
server.tool("classify",
  "task 난이도/의도를 미리보기. 실행하지 않고 어떤 모드/엔진이 선택될지 확인.",
  {
    task: z.string().describe("Task description to classify"),
    scope: z.enum(["small", "medium", "large"]).optional(),
    risk: z.enum(["low", "medium", "high"]).optional(),
    artifact_type: z.enum(["none", "ppt", "pdf", "doc"]).optional(),
  },
  async (args) => {
    try {
      const r = await flask("/api/horcrux/classify", "POST", {
        task: args.task,
        scope: args.scope || "medium",
        risk: args.risk || "medium",
        artifact_type: args.artifact_type || "none",
      });
      let t = `## Task Classification\n\n`;
      t += `**Mode:** ${r.recommended_mode}\n`;
      t += `**Engine:** ${r.internal_engine}\n`;
      t += `**Intent:** ${r.detected_intent}\n`;
      t += `**Confidence:** ${((r.confidence || 0) * 100).toFixed(0)}%\n`;
      t += `**Source:** ${r.routing_source}\n`;
      t += `**Reason:** ${r.reason}\n`;
      if (r.stages && r.stages.length) t += `**Stages:** ${r.stages.join(" → ")}\n`;
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

// ─── 4. analytics: 운영 데이터 대시보드 ───
server.tool("analytics",
  "운영 데이터 대시보드. timeout 통계, 모드별 성능, critic 신뢰도 조회.",
  {
    section: z.enum(["all", "timeouts", "modes", "critics", "heuristic"]).optional().describe("Which section to view (default: all)"),
  },
  async (args) => {
    try {
      const section = args.section || "all";
      let r;

      if (section === "timeouts") {
        r = await flask("/api/analytics/timeouts");
        let t = `## Timeout Statistics (P50/P90/P99)\n\n`;
        for (const [stage, stats] of Object.entries(r)) {
          t += `**${stage}:** P50=${stats.p50}ms P90=${stats.p90}ms P99=${stats.p99}ms (n=${stats.count}) → recommended: ${stats.recommended_timeout_ms}ms\n`;
        }
        return { content: [{ type: "text", text: t || "No latency data yet. Run some tasks first." }] };

      } else if (section === "modes") {
        r = await flask("/api/analytics/modes");
        let t = `## Mode Usage Stats\n\n`;
        for (const [mode, stats] of Object.entries(r)) {
          t += `**${mode}:** ${stats.usage_count} runs | avg_score=${stats.avg_score} | avg_latency=${Math.round(stats.avg_latency_ms)}ms | convergence=${(stats.convergence_rate * 100).toFixed(0)}%\n`;
        }
        return { content: [{ type: "text", text: t || "No mode data yet." }] };

      } else if (section === "critics") {
        r = await flask("/api/analytics/critics");
        let t = `## Critic Reliability\n\n`;
        for (const [model, stats] of Object.entries(r)) {
          t += `**${model}:** reliability=${stats.reliability_score} | weight=${stats.recommended_weight} | reviews=${stats.total_reviews} | avg_delta=${stats.avg_score_delta}\n`;
        }
        return { content: [{ type: "text", text: t || "No critic data yet." }] };

      } else if (section === "heuristic") {
        r = await flask("/api/analytics/heuristic");
        let t = `## Heuristic Refinement Suggestions\n\n`;
        for (const s of (r.suggestions || [])) {
          t += `- ${s}\n`;
        }
        return { content: [{ type: "text", text: t || "No suggestions yet. Need more run data." }] };

      } else {
        // all
        r = await flask("/api/analytics");
        let t = `## Horcrux Analytics Dashboard\n\n`;
        t += `**Total sessions:** ${r.total_sessions || 0} | **Total stages logged:** ${r.total_stages_logged || 0}\n\n`;

        t += `### Mode Usage\n`;
        for (const [mode, stats] of Object.entries(r.mode_stats || {})) {
          t += `- ${mode}: ${stats.usage_count} runs, avg=${stats.avg_score}/10, ${Math.round(stats.avg_latency_ms)}ms\n`;
        }

        t += `\n### Timeout Recommendations\n`;
        for (const [key, val] of Object.entries(r.timeout_recommendations || {})) {
          t += `- ${key}: ${val}ms\n`;
        }

        t += `\n### Critic Reliability\n`;
        for (const [model, stats] of Object.entries(r.critic_reliability || {})) {
          t += `- ${model}: reliability=${stats.reliability_score}, weight=${stats.recommended_weight}\n`;
        }

        const suggestions = r.heuristic_refinements?.suggestions || [];
        if (suggestions.length) {
          t += `\n### Heuristic Suggestions\n`;
          for (const s of suggestions) t += `- ${s}\n`;
        }

        return { content: [{ type: "text", text: t }] };
      }
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

// ─── 5. feedback: Interactive session 피드백 ───
server.tool("feedback",
  "Horcrux interactive session에 피드백 주입. paused 상태에서 continue/feedback/focus/stop/rollback 가능.",
  {
    job_id: z.string().describe("Interactive session job ID"),
    action: z.enum(["continue", "feedback", "focus", "stop", "rollback"]).describe("수행할 액션"),
    message: z.string().optional().describe("feedback/focus 시 자연어 지시"),
    rollback_to: z.number().optional().describe("rollback 시 되돌릴 라운드 번호"),
  },
  async (args) => {
    try {
      const body = { job_id: args.job_id, action: args.action };
      if (args.message) {
        if (args.action === "focus") body.focus_area = args.message;
        else body.human_directive = args.message;
      }
      if (args.rollback_to) body.rollback_to_round = args.rollback_to;

      const r = await flask("/api/horcrux/feedback", "POST", body);
      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };

      let t = `## Feedback Applied\n\n`;
      t += `**Status:** ${r.status}\n`;
      t += `**Message:** ${r.message || ""}\n`;
      if (r.next_round) t += `**Next Round:** ${r.next_round}\n`;
      if (r.irreversible_warning) t += `\n⚠️ **Warning:** ${r.irreversible_warning}\n`;
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

// ─── 6. horcrux_test: AI 연결 테스트 ───
server.tool("horcrux_test",
  "AI 연결 테스트 (Claude, Codex)",
  {},
  async () => {
    try {
      const r = await flask("/api/test");
      let t = "Connection Test:\n";
      for (const [n, v] of Object.entries(r))
        t += (v.ok ? "✅" : "❌") + " " + n + " " + (v.json ? "JSON ok" : "no JSON") + "\n";
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

// ─── Main ───
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write("MCP: horcrux v8.0 connected\n");
  process.stderr.write("  Tools: run, check, classify, analytics, feedback, horcrux_test\n");
}

main().catch(e => { process.stderr.write("MCP fatal: " + e.stack + "\n"); process.exit(1); });
