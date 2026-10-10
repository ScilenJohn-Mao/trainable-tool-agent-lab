import type { TaskStatus } from './api/types';
export const statusNames: Record<TaskStatus, string> = {
  queued: '排队中', running: '处理中', waiting_input: '待补充信息', waiting_approval: '待确认',
  completed: '已完成', failed: '执行失败', cancelled: '已取消',
};
export const actionNames: Record<string, string> = {
  request_refund: '模拟退款', issue_coupon: '延迟补偿券', create_handoff: '转人工',
  get_order: '查询订单', search_policy: '检索政策', read_policy: '读取条款', get_operation: '核对操作',
};
export const money = (fen: number) => `¥${(fen / 100).toFixed(2)}`;
export const dateTime = (date: string) => new Date(date).toLocaleString('zh-CN', { hour12: false });
export const terminal = (status: TaskStatus) => ['completed', 'failed', 'cancelled'].includes(status);
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : '请求未完成，请刷新核对当前状态';
