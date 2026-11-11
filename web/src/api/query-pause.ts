import { useSyncExternalStore } from 'react';

/**
 * 连接设置面板打开期间，暂缓页面业务查询。
 *
 * 目的：面板里粘令牌 → 会话返回 → 面板显示「已从会话带出」，但此时用户还没点保存，
 * 页面不能拿还没确认的范围去取数。面板只保留会话查询本身，其余业务查询等面板关闭后再发。
 */
let paused = false;
const listeners = new Set<() => void>();

export function setBusinessQueriesPaused(value: boolean): void {
  if (paused === value) return;
  paused = value;
  listeners.forEach((listener) => listener());
}

export function isBusinessQueriesPaused(): boolean {
  return paused;
}

export function useBusinessQueriesPaused(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => paused,
    () => paused,
  );
}
