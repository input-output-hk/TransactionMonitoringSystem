import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva(
  'inline-flex items-center justify-center rounded-md border px-2.5 py-0.5 text-xs font-bold uppercase tracking-wide',
  {
    variants: {
      variant: {
        default:
          'border-transparent bg-secondary text-secondary-foreground',
        outline: 'border-border text-foreground',
        low: 'border-severity-low-foreground/30 bg-severity-low/60 text-severity-low-foreground',
        medium:
          'border-severity-medium-foreground/30 bg-severity-medium/60 text-severity-medium-foreground',
        high: 'border-severity-high-foreground/30 bg-severity-high/60 text-severity-high-foreground',
        critical:
          'border-severity-critical-foreground/30 bg-severity-critical/60 text-severity-critical-foreground',
      },
    },
    defaultVariants: { variant: 'default' },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props} />
  )
}
