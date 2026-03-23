#!/usr/bin/env node
/**
 * Debate Chain MCP Server v7
 * - run: debate / pair2 / pair3 / debate_pair2 / debate_pair3 통합 실행
 * - check: 모든 job 상태 확인 + 완료 시 자동 결과 포함
 * - self_improve: 자기개선 루프
 * - 기존 도구 하위 호환 유지
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
      headers: { "Content-Type": "application/json" },
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

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const server = new McpServer({ name: "debate-chain", version: "7.0" });

// ─── 1. run: 통합 실행 도구 ───
server.tool("run",
  "Run debate, pair, or debate→pair pipeline. Modes: debate, pair2, pair3, debate_pair2, debate_pair3",
  {
    task: z.string().describe("The task to run"),
    mode: z.enum(["debate", "pair2", "pair3", "debate_pair2", "debate_pair3"]).default("debate"),
    max_rounds: z.number().optional(),
    threshold: z.number().optional(),
    parent_debate_id: z.string().optional().describe("Deep Dive: chain from a previous debate's final_solution"),
  },
  async (args) => {
    try {
      const mode = args.mode || "debate";
      let r, job_id, msg;

      if (mode === "debate") {
        r = await flask("/api/start", "POST", {
          task: args.task,
          max_rounds: args.max_rounds || 5,
          threshold: args.threshold || 8.0,
          parent_debate_id: args.parent_debate_id || "",
        });
        job_id = r.debate_id;
        msg = `debate_id: ${job_id}\nUse check("${job_id}") to monitor.`;

      } else if (mode.startsWith("debate_pair")) {
        const pairMode = mode.replace("debate_", ""); // pair2 or pair3
        r = await flask("/api/debate_pair", "POST", {
          task: args.task,
          pair_mode: pairMode,
          threshold: args.threshold || 8.0,
          max_rounds: args.max_rounds || 3
        });
        job_id = r.pipeline_id;
        msg = `pipeline_id: ${job_id}\ndebate → ${pairMode} pipeline started.\nUse check("${job_id}") to monitor.`;

      } else {
        // pair2, pair3
        r = await flask("/api/pair", "POST", { task: args.task, mode: mode });
        job_id = r.pair_id;
        msg = `pair_id: ${job_id}\nUse check("${job_id}") to monitor.`;
      }

      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };
      return { content: [{ type: "text", text: msg }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

// ─── 2. check: 통합 상태 확인 + 완료 시 자동 결과 ───
server.tool("check",
  "Check status of any job (debate, pair, pipeline). Auto-includes result when done.",
  { job_id: z.string() },
  async (args) => {
    try {
      const id = args.job_id;
      let status, resultEndpoint;

      // pipeline (dp_)
      if (id.startsWith("dp_")) {
        status = await flask(`/api/pipeline/status/${id}`);
        if (status.error) return { content: [{ type: "text", text: "Not found: " + id }] };
        if (status.status === "running") {
          return { content: [{ type: "text", text: `pipeline: ${status.status} | phase: ${status.phase} | debate: ${status.debate_id || "pending"} | pair: ${status.pair_id || "pending"}` }] };
        }
        // 완료
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
        status = await flask(`/api/pair/status/${id}`);
        if (status.error) return { content: [{ type: "text", text: "Not found: " + id }] };
        if (status.status === "running") {
          return { content: [{ type: "text", text: `pair: ${status.status} | phase: ${status.phase} | parts_done: ${status.parts_done || 0}` }] };
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

      // self_improve (si_)
      if (id.startsWith("si_")) {
        status = await flask(`/api/self_improve/status/${id}`);
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
      status = await flask(`/api/status/${id}`);
      if (status.error) return { content: [{ type: "text", text: "Not found: " + id }] };
      if (status.status === "running") {
        return { content: [{ type: "text", text: `debate: Round ${status.round} | Score: ${(status.avg_score || 0).toFixed(1)}/10 | phase: ${status.phase || "..."}` }] };
      }
      // 완료 — full result
      const full = await flask(`/api/result/${id}`);
      let t = `## ${full.status === "converged" ? "✅ Converged" : "⚠️ " + full.status}\n`;
      t += `Rounds: ${full.round} | Score: ${(full.avg_score || 0).toFixed(1)}/10\n\n`;
      // 마지막 라운드 메시지만
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

// ─── 3. self_improve ───
server.tool("self_improve",
  "Self-improvement loop: iteratively improve a solution (optionally based on a previous debate result)",
  {
    task: z.string(),
    iterations: z.number().optional(),
    debate_id: z.string().optional().describe("Optional: use final_solution from this debate as starting point"),
  },
  async (args) => {
    try {
      const r = await flask("/api/self_improve", "POST", {
        task: args.task,
        iterations: args.iterations || 3,
        debate_id: args.debate_id,
      });
      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };
      return { content: [{ type: "text", text: `self_improve_id: ${r.self_improve_id}\nUse check("${r.self_improve_id}") to monitor.` }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

// ─── 4. test ───
server.tool("debate_test",
  "Test AI connections (claude, codex)",
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

// ─── 하위 호환: 기존 도구들 유지 ───

server.tool("debate_start",
  "Start a debate (legacy). Use 'run' tool instead.",
  { task: z.string(), max_rounds: z.number().optional(), threshold: z.number().optional() },
  async (args) => {
    try {
      const r = await flask("/api/start", "POST", {
        task: args.task, max_rounds: args.max_rounds || 5, threshold: args.threshold || 8.0
      });
      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };
      return { content: [{ type: "text", text: `debate_id: ${r.debate_id}\nUse check("${r.debate_id}") or debate_status("${r.debate_id}") to monitor.` }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

server.tool("debate_status",
  "Check debate status (legacy). Use 'check' tool instead.",
  { debate_id: z.string() },
  async (args) => {
    try {
      const r = await flask("/api/status/" + args.debate_id);
      if (r.error) return { content: [{ type: "text", text: "Not found" }] };
      const done = r.status !== "running";
      let t = `status: ${r.status} | Round: ${r.round} | Score: ${(r.avg_score || 0).toFixed(1)}/10 | phase: ${r.phase || "-"}`;
      if (done) t += `\n\nDone! Use check("${args.debate_id}") to get the full solution.`;
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

server.tool("debate_result",
  "Get debate result (legacy). Use 'check' tool instead.",
  { debate_id: z.string() },
  async (args) => {
    try {
      const status = await flask("/api/status/" + args.debate_id);
      if (status.error) return { content: [{ type: "text", text: "Not found" }] };
      if (status.status === "running") {
        return { content: [{ type: "text", text: `Still running R${status.round} ${(status.avg_score || 0).toFixed(1)}/10 — check again later.` }] };
      }
      const res = await flask("/api/result/" + args.debate_id);
      let t = `## ${res.status === "converged" ? "✅ Converged" : "⚠️ " + res.status}\n`;
      t += `Rounds: ${res.round} | Score: ${(res.avg_score || 0).toFixed(1)}/10\n\n`;
      const msgs = res.messages || [];
      const lastGenIdx = msgs.reduce((li, m, i) => m.role === "generator" ? i : li, 0);
      for (const m of msgs.slice(lastGenIdx)) {
        t += `**${m.role}**`;
        if (m.score !== undefined) t += ` (${m.score.toFixed(1)}/10)`;
        t += `\n${(m.content || "").slice(0, 2000)}\n\n`;
      }
      if (res.final_solution) t += `## Final Solution\n${res.final_solution.slice(0, 4000)}`;
      if (t.length > 10000) t = t.slice(0, 10000) + "\n\n[...truncated]";
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

server.tool("pair2_start",
  "2-AI parallel gen (legacy). Use 'run' with mode='pair2' instead.",
  { task: z.string(), context: z.string().optional() },
  async (args) => {
    try {
      const r = await flask("/api/pair", "POST", { task: args.task, mode: "pair2", context: args.context || "" });
      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };
      return { content: [{ type: "text", text: `pair_id: ${r.pair_id}\nUse check("${r.pair_id}") to monitor.` }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

server.tool("pair3_start",
  "3-AI parallel gen (legacy). Use 'run' with mode='pair3' instead.",
  { task: z.string(), context: z.string().optional() },
  async (args) => {
    try {
      const r = await flask("/api/pair", "POST", { task: args.task, mode: "pair3", context: args.context || "" });
      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };
      return { content: [{ type: "text", text: `pair_id: ${r.pair_id}\nUse check("${r.pair_id}") to monitor.` }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

server.tool("pair_status",
  "Check pair status (legacy). Use 'check' tool instead.",
  { pair_id: z.string() },
  async (args) => {
    try {
      const r = await flask("/api/pair/status/" + args.pair_id);
      if (r.error) return { content: [{ type: "text", text: "Not found" }] };
      const done = r.status !== "running";
      let t = `status: ${r.status} | phase: ${r.phase} | parts_done: ${r.parts_done || 0}`;
      if (done) t += `\n\nDone! Use check("${args.pair_id}") to get the full result.`;
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

server.tool("pair_result",
  "Get pair result (legacy). Use 'check' tool instead.",
  { pair_id: z.string() },
  async (args) => {
    try {
      const status = await flask("/api/pair/status/" + args.pair_id);
      if (status.error) return { content: [{ type: "text", text: "Not found" }] };
      if (status.status === "running") {
        return { content: [{ type: "text", text: `Still running phase=${status.phase} — check again later.` }] };
      }
      const res = await flask("/api/pair/result/" + args.pair_id);
      let t = `## Pair ${(res.mode || "").toUpperCase()} ${res.status}\n\n`;
      t += `### Spec\n${res.spec || "N/A"}\n\n`;
      for (const msg of (res.messages || [])) {
        if (msg.role === "architect") continue;
        t += `### ${msg.role} (${msg.model || "?"})\n${(msg.content || "").slice(0, 3000)}\n\n`;
      }
      if (t.length > 12000) t = t.slice(0, 12000) + "\n\n[...truncated]";
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

// ─── Main ───
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write("MCP: debate-chain v7.0 connected\n");
  process.stderr.write("  Tools: run, check, self_improve, debate_test\n");
  process.stderr.write("  Legacy: debate_start/status/result, pair2/3_start, pair_status/result\n");
}

main().catch(e => { process.stderr.write("MCP fatal: " + e.stack + "\n"); process.exit(1); });
