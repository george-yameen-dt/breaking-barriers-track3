import { sendIntent } from "@dynatrace-sdk/navigation";
import { num, str, type Row } from "./hooks";

export interface Problem {
  id: string;
  displayId: string;
  active: boolean;
  start: number;
  end: number;
  description: string;
}

/** The problem description is markdown; keep only the sentence the OpenPipeline rule wrote. */
function summary(md: string): string {
  const line = md.split("\n").find((l) => /without signal/.test(l));
  return line ? line.replace(/^[-*\s]+/, "").trim() : "";
}

export function toProblem(r: Row): Problem | undefined {
  const id = str(r["event.id"]);
  if (!id) return undefined;
  return {
    id,
    displayId: str(r.display_id, "Problem"),
    active: str(r["event.status"]) === "ACTIVE",
    start: Date.parse(str(r["event.start"])) || 0,
    end: Date.parse(str(r["event.end"])) || 0,
    description: summary(str(r["event.description"])),
  };
}

export interface Topology {
  networkId?: string;
  /** Smartscape node id by tower letter ("A") and by phone id ("phone-07"). */
  towers: Map<string, string>;
  phones: Map<string, string>;
  links: number;
}

export function toTopology(nodes: Row[], links: Row[]): Topology {
  const towers = new Map<string, string>();
  const phones = new Map<string, string>();
  let networkId: string | undefined;
  for (const n of nodes) {
    const type = str(n.type);
    const id = str(n.id);
    const name = str(n.name);
    if (type === "CUSTOM_CELL_NETWORK") networkId = id;
    else if (type === "CUSTOM_CELL_TOWER") towers.set(name.replace(/^Tower /, ""), id);
    else if (type === "CUSTOM_MOBILE_PHONE") phones.set(name, id);
  }
  return { networkId, towers, phones, links: num(links[0]?.links) };
}

export const phoneKey = (id: number) => `phone-${String(id).padStart(2, "0")}`;

/** Opens a Smartscape node in the Smartscape app's topology view. */
export function openInSmartscape(id: string) {
  sendIntent({ id }, { recommendedAppId: "dynatrace.smartscape", recommendedIntentId: "view_topology_in_context" });
}

export function openProblem(p: Problem) {
  sendIntent({ "event.id": p.id, "event.kind": "DAVIS_PROBLEM" }, { recommendedAppId: "dynatrace.davis.problems", recommendedIntentId: "view-problem" });
}

/** The problem graph in Smartscape: what the problem touches. */
export function openProblemGraph(p: Problem) {
  sendIntent({ "event.id": p.id, "event.kind": "DAVIS_PROBLEM" }, { recommendedAppId: "dynatrace.smartscape", recommendedIntentId: "view_problem" });
}
