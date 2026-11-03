import { useCountUp } from '../lib/motion'

/** 指标格里的滚动数字：hooks 规则要求无条件调用，
 *  所以做成子组件——每格自己挂一份 hook。
 *  P3 起为 ReportView/WorkflowView 共用（原来私活在 WorkflowView 里）。 */
export function CountUp({ v, fmt }: { v: number; fmt?: (n: number) => string }) {
  const n = useCountUp(v)
  return <>{fmt ? fmt(n) : Math.round(n)}</>
}
