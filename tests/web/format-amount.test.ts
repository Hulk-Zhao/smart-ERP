import { describe, expect, it } from 'vitest';
import { amount, money, moneyText } from '../../web/src/lib/format';

describe('金额格式统一：千分位 + 两位小数', () => {
  it('后端 Decimal 原值不再原样透出 4 位小数', () => {
    expect(amount('735000.0000')).toBe('735,000.00');
    expect(amount('355000.0000')).toBe('355,000.00');
    expect(amount('7108301.1000')).toBe('7,108,301.10');
    expect(money('512000.0000')).toBe('512,000.00 CNY');
  });

  it('文案里的 Decimal 被改写，工单号/账期等文本不受影响', () => {
    expect(moneyText('金额：735000.0000')).toBe('金额：735,000.00');
    expect(moneyText('TK-202609-0012 期 2026-10')).toBe('TK-202609-0012 期 2026-10');
  });
});
