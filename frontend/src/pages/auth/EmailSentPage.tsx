import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/lib/auth-context'

export function EmailSentPage() {
  const navigate = useNavigate()
  const { resendEmail, verifyEmail, user } = useAuth()
  const [resending, setResending] = useState(false)
  const [resent, setResent] = useState(false)

  async function onResend() {
    setResending(true)
    try {
      await resendEmail()
      setResent(true)
    } finally {
      setResending(false)
    }
  }

  // Mock: clicking the title 3x simulates verification (since there's no real email)
  async function onMockVerify() {
    await verifyEmail()
    navigate('/dashboard')
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-[460px] rounded-2xl border border-border bg-card p-10 shadow-sm">
        <h1
          onClick={onMockVerify}
          title="(mock) click to verify"
          className="mb-12 cursor-pointer select-none text-center text-4xl font-extrabold tracking-tight text-foreground"
        >
          TMS
        </h1>

        <p className="text-center text-sm leading-relaxed text-foreground">
          An email has been successfully sent
          {user?.email ? ` to ${user.email}` : ''}.
          <br />
          If you did not receive please click resend.
        </p>

        <div className="mt-16 flex justify-center">
          <Button
            type="button"
            variant="outline"
            onClick={onResend}
            disabled={resending}
            className="min-w-[140px]"
          >
            {resending ? 'Sending…' : resent ? 'Sent ✓' : 'Resend'}
          </Button>
        </div>
      </div>
    </div>
  )
}
