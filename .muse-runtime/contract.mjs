// MSP shapes verified against Muse Code 1.4.10's embedded schema.
export function assertReady(init, account, needsMcp) {
  if (account.state !== 'accountLogin') throw new Error('Muse subscription account login is required; API fallback is disabled');
  if (init.sandboxAvailability !== 'available') throw new Error(`Muse sandbox is ${init.sandboxAvailability ?? 'unverified'} in this execution environment`);
  if (init.schema?.version !== 1 || init.sessionDurability !== 'durable') throw new Error('Muse requires MSP v1 durable sessions');
  if (needsMcp && !init.grantedCapabilities?.includes('sessionMcp')) throw new Error('Muse did not grant sessionMcp');
}

export function verifySession(session, spec) {
  if (spec.engine_session_id && session.sessionId !== spec.engine_session_id) throw new Error('Muse resume identity mismatch');
  if (session.workspaceRoot !== spec.cwd) throw new Error('Muse resume workspace mismatch');
  if (spec.model && session.modelId !== spec.model) throw new Error('Muse effective model differs from requested model');
}

export function sessionParams(spec) {
  const servers = Object.fromEntries(Object.entries(spec.mcp_servers ?? {}).map(([name, srv]) => [name, {
    transport: 'stdio', command: srv.command, args: srv.args ?? [], env: srv.env ?? {}, mode: 'required',
  }]));
  return {
    workspaceRoot: spec.cwd,
    workspaceRoots: [...new Set([spec.cwd, ...spec.write_roots])],
    modelId: spec.model,
    approvalMode: 'allowAll',
    ...(Object.keys(servers).length ? {config:{mcpServers:servers}} : {}),
  };
}
