import { vi } from 'vitest';

// Gives a fake mysql2 pool the getConnection() path the adapter uses. Session setup
// (SET SESSION …) is answered here, so `pool.query` sees only the agent's statements.
export function withConnections(pool, { serverVersion = '8.0.36' } = {}) {
  const connection = {
    threadId: 42,
    connection: { _handshakePacket: { serverVersion } },
    query: vi.fn(request => (String(request).startsWith('SET SESSION')
      ? Promise.resolve([{}])
      : pool.query(request))),
    release: vi.fn(),
    destroy: vi.fn(),
  };
  pool.connection = connection;
  pool.getConnection = vi.fn().mockResolvedValue(connection);
  return pool;
}
