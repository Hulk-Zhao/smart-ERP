/** 打开连接设置面板的事件名；错误态的「打开连接设置」由 AppShell 统一监听。 */
export const OPEN_SETTINGS_EVENT = 'smart-erp:open-settings';

export function openSettingsPanel(): void {
  window.dispatchEvent(new Event(OPEN_SETTINGS_EVENT));
}
