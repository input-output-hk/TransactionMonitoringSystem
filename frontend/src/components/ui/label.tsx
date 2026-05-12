import { cn } from '@/lib/utils'

export function Label({
  className,
  ...props
}: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn(
        'text-sm font-semibold leading-none text-foreground',
        className
      )}
      {...props}
    />
  )
}
