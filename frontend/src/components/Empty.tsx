import { Icon, type IconName } from './Icon'

/** 统一空态：图标 + 一句话。
 *  原来各处空态都是裸的一行 `text-xs text-muted`，视觉上像
 *  「忘了渲染」而不是「确实没有」。图标立起来才是个状态。 */
export function Empty({ icon, text }: { icon: IconName; text: string }) {
  return (
    <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
      <Icon name={icon} size={22} strokeWidth={1.25} className="text-muted/45" />
      <p className="text-xs leading-5 text-muted">{text}</p>
    </div>
  )
}
