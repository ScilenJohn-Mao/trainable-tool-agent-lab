import { test, expect } from '@playwright/test';

test('manual ticket flow uses persisted receipts and policy citations', async ({ page, request }, testInfo) => {
  const decision = process.env.TTAL_BROWSER_DECISION || 'approved';
  const exceptions: string[] = [];
  page.on('pageerror', error => exceptions.push(error.message));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '每一笔处理，都有依据。' })).toBeVisible();
  await page.getByLabel('售后问题').fill('收到的商品已损坏，请处理售后');
  await page.getByLabel('订单号', { exact: false }).fill('MISSING');
  await page.getByRole('button', { name: '创建工单', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('订单不存在');
  await page.getByLabel('订单号', { exact: false }).fill('');
  await page.getByRole('button', { name: '创建工单', exact: true }).click();
  await expect(page.getByRole('heading', { name: '补充订单信息' })).toBeVisible();
  const taskId = new URL(page.url()).searchParams.get('task')!;
  let detail = await (await request.get(`/api/tasks/${taskId}`)).json();
  const thread = detail.attempt.thread_id;
  await page.reload();
  await expect(page.getByRole('heading', { name: '补充订单信息' })).toBeVisible();
  if (decision === 'cancelled') {
    await page.getByRole('button', { name: '取消工单', exact: true }).click();
    await expect(page.getByText('此工单已取消。取消不会撤销此前已经提交的业务。')).toBeVisible();
  } else {
    await page.getByLabel('你的回复').fill('ORD-1001');
    await page.getByRole('button', { name: '提交补充信息', exact: true }).click();
    await expect(page.getByRole('heading', { name: '确认处理方案' })).toBeVisible();
    await expect(page.getByText('¥129.00', { exact: true })).toBeVisible();
    detail = await (await request.get(`/api/tasks/${taskId}`)).json();
    expect(detail.status).toBe('waiting_approval');
    expect(detail.result).toBeNull();
    expect(detail.attempt.thread_id).toBe(thread);
    const proposal = await (await request.get(`/api/tasks/${taskId}/proposal`)).json();
    expect(proposal.parameters.amount_minor).toBe(12900);
    await page.screenshot({ path: testInfo.outputPath('proposal.png'), fullPage: true });
    await page.getByRole('button', { name: decision === 'approved' ? '确认执行' : '拒绝方案', exact: true }).click();
    await expect(page.getByRole('heading', { name: decision === 'approved' ? '处理已完成' : '方案已拒绝', exact: true })).toBeVisible();
    detail = await (await request.get(`/api/tasks/${taskId}`)).json();
    expect(detail.attempt.thread_id).toBe(thread);
    expect(detail.result.operations.length).toBe(decision === 'approved' ? 1 : 0);
    if (decision === 'approved') {
      expect(detail.result.operations[0].amount_minor).toBe(12900);
      await expect(page.getByText(detail.result.operations[0].operation_key, { exact: true })).toBeVisible();
    }
    await page.getByRole('button', { name: /P-REFUND-ELIGIBILITY.*eligibility/ }).click();
    await expect(page.locator('.policy-text')).toContainText('damage_verified=true');
    await page.reload();
    await expect(page.getByRole('heading', { name: decision === 'approved' ? '处理已完成' : '方案已拒绝', exact: true })).toBeVisible();
    await page.getByRole('region', { name: '执行进展' }).getByText('查看工具参数').first().click();
    await expect(page.getByRole('region', { name: '执行进展' }).getByText('查看工具结果').first()).toBeVisible();
    if (decision === 'approved') {
      await page.route(`**/api/tasks/${taskId}/events`, route => route.abort());
      await page.reload();
      await expect(page.getByRole('button', { name: '重新连接', exact: true })).toBeVisible();
      await page.unroute(`**/api/tasks/${taskId}/events`);
      await page.getByRole('button', { name: '重新连接', exact: true }).click();
      await expect(page.getByText('已接收全部进展', { exact: true })).toBeVisible();
      await expect(page.getByRole('heading', { name: '处理已完成', exact: true })).toBeVisible();
    }
  }
  await page.screenshot({ path: testInfo.outputPath('result.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('heading', { name: '新建工单', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(exceptions).toEqual([]);
});
