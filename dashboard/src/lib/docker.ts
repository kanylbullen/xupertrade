/**
 * Thin wrapper over dockerode for the bot-orchestration use case.
 *
 * Bind-mount `/var/run/docker.sock` from the host into the dashboard
 * container; the wrapper talks to the host's Docker daemon via that
 * socket. Same blast-radius as the operator already has on the host
 * (Docker socket = root-equivalent), so no new privilege boundary.
 *
 * The interface here is intentionally small — just create / start /
 * stop / remove / inspect / list. Everything specific to "what env
 * vars does a bot need" lives in `bot-orchestrator.ts`, which uses
 * this wrapper.
 */

import Docker from "dockerode";

const SOCKET_PATH = process.env.DOCKER_SOCKET ?? "/var/run/docker.sock";

let _client: Docker | null = null;
function getClient(): Docker {
  if (_client === null) _client = new Docker({ socketPath: SOCKET_PATH });
  return _client;
}

/**
 * Override the Docker client (for tests). Pass `null` to revert to
 * the lazily-instantiated default.
 */
export function setDockerClient(client: Docker | null): void {
  _client = client;
}

export type ContainerSpec = {
  /** Container name, e.g. `hypertrade-bot-3a2f1e4c-mainnet`. */
  name: string;
  /** Image to run, e.g. `hypertrade-bot:latest`. */
  image: string;
  /** Env vars as `KEY=value` strings (Docker convention). */
  env: string[];
  /** Docker network the container joins (must exist already). */
  networkName: string;
  /** Memory cap in bytes; 0 = unlimited. */
  memoryBytes?: number;
  /** CPU cap as nano-CPUs (1e9 = 1 CPU); 0 = unlimited. */
  nanoCpus?: number;
  /** Restart policy. Default `unless-stopped` matches the operator's bots. */
  restartPolicy?: "no" | "always" | "unless-stopped" | "on-failure";
  /** Optional labels (e.g. `{tenant_id: "...", bot_id: "...", mode: "mainnet"}`). */
  labels?: Record<string, string>;
};

export type ContainerInfo = {
  id: string;
  name: string;
  image: string;
  state: string;        // 'running' | 'exited' | 'created' | ...
  status: string;       // human-readable: "Up 2 minutes"
  startedAt?: string;   // ISO-8601 timestamp
  labels: Record<string, string>;
};

/**
 * Create + start a container in one call. Returns the container info
 * so the caller can persist `id` + `name` to the DB. Throws on any
 * Docker error (caller maps to 5xx).
 */
export async function createAndStart(spec: ContainerSpec): Promise<ContainerInfo> {
  const client = getClient();
  const container = await client.createContainer({
    name: spec.name,
    Image: spec.image,
    Env: spec.env,
    Labels: spec.labels ?? {},
    HostConfig: {
      NetworkMode: spec.networkName,
      RestartPolicy: { Name: spec.restartPolicy ?? "unless-stopped" },
      ...(spec.memoryBytes ? { Memory: spec.memoryBytes } : {}),
      ...(spec.nanoCpus ? { NanoCpus: spec.nanoCpus } : {}),
    },
  });
  await container.start();
  return inspectContainer(container.id);
}

/** Stop + remove a container by id. Idempotent on already-gone. */
export async function stopAndRemove(containerId: string): Promise<void> {
  const client = getClient();
  const container = client.getContainer(containerId);
  try {
    await container.stop({ t: 10 });  // 10s grace, matches our SIGTERM handler
  } catch (err) {
    // Already stopped — fine. Anything else propagates.
    if (!isNotFoundOrAlreadyStopped(err)) throw err;
  }
  try {
    await container.remove({ force: true });
  } catch (err) {
    if (!isNotFound(err)) throw err;
  }
}

/**
 * Inspect a container by id. Throws on docker errors; the caller is
 * responsible for translating 404 (container doesn't exist) into
 * whatever response shape it wants. The orchestrator's `statusBot`
 * does this — see bot-orchestrator.ts.
 */
export async function inspectContainer(containerId: string): Promise<ContainerInfo> {
  const client = getClient();
  const container = client.getContainer(containerId);
  const info = await container.inspect();
  return {
    id: info.Id,
    name: (info.Name ?? "").replace(/^\//, ""),
    image: info.Config.Image,
    state: info.State.Status,
    status: humanStatus(info.State),
    startedAt: info.State.StartedAt,
    labels: info.Config.Labels ?? {},
  };
}

/**
 * Find a container by exact name. Returns null if not found.
 * Useful to detect "is this tenant_bots row's container actually
 * still alive?" in reconcile passes.
 */
export async function findByName(name: string): Promise<ContainerInfo | null> {
  const client = getClient();
  const list = await client.listContainers({
    all: true,
    filters: { name: [name] },
  });
  // Docker's name filter is a substring match; require exact.
  const exact = list.find((c) =>
    (c.Names ?? []).some((n) => n.replace(/^\//, "") === name),
  );
  if (!exact) return null;
  return inspectContainer(exact.Id);
}

function humanStatus(state: { Status: string; StartedAt?: string }): string {
  if (state.Status === "running" && state.StartedAt) {
    return `Up since ${state.StartedAt}`;
  }
  return state.Status;
}

function isNotFound(err: unknown): boolean {
  // dockerode wraps Docker API errors with a `statusCode` field.
  return (
    typeof err === "object" &&
    err !== null &&
    "statusCode" in err &&
    (err as { statusCode: number }).statusCode === 404
  );
}

function isNotFoundOrAlreadyStopped(err: unknown): boolean {
  if (isNotFound(err)) return true;
  return (
    typeof err === "object" &&
    err !== null &&
    "statusCode" in err &&
    (err as { statusCode: number }).statusCode === 304
  );
}
