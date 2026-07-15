import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import { TableFooter } from "@/components/ui/table-footer";
import {
	createUser,
	deleteUser,
	listUsers,
	resendInvite,
	type User as ApiUser,
	type UserRole,
} from "@/lib/api/auth";
import { DEFAULT_PAGE_SIZE } from "@/lib/constants";
import { initials } from "@/lib/utils/strings";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Minus, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

const USER_ROLES: UserRole[] = ["Admin", "Reviewer"];

const USERS_QUERY_KEY = ["users", "list"] as const;

export function UsersPage() {
	const [page, setPage] = useState(0);
	const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
	const [removeMode, setRemoveMode] = useState(false);
	const [addOpen, setAddOpen] = useState(false);
	const [pendingRemove, setPendingRemove] = useState<ApiUser | null>(null);
	const qc = useQueryClient();

	// Server-side pagination via `/api/v1/users?limit&offset` — see backend
	// list_users for the {count,total,data} shape.
	const { data, isPending, isError } = useQuery({
		queryKey: [...USERS_QUERY_KEY, page, pageSize],
		queryFn: () => listUsers({ limit: pageSize, offset: page * pageSize }),
		staleTime: 30_000,
	});

	const rows = data?.data ?? [];
	const total = data?.total ?? 0;
	const pageCount = Math.max(1, Math.ceil(total / pageSize));
	const currentPage = Math.min(page, pageCount - 1);

	// Snap back to a populated page whenever the current one ends up empty
	// (last row deleted, or rows removed elsewhere). Done as a render-phase
	// update rather than in an effect: React applies it before committing,
	// so there's no cascading-render warning and no flash of "No users".
	// Guarded so it converges to 0 without looping; `data &&` skips the
	// in-flight window where a page change has reset `data` to undefined.
	if (data && data.data.length === 0 && page > 0) {
		setPage((p) => Math.max(0, p - 1));
	}

	const removeMutation = useMutation({
		mutationFn: (id: string) => deleteUser(id),
		onSuccess: () => {
			void qc.invalidateQueries({ queryKey: USERS_QUERY_KEY });
			toast.success("User removed");
		},
		onError: (err) =>
			toast.error(err instanceof Error ? err.message : "Remove failed"),
	});

	const resendMutation = useMutation({
		mutationFn: (id: string) => resendInvite(id),
		onSuccess: () =>
			toast.success("Invitation link resent — expires in 15 minutes."),
		onError: (err) =>
			toast.error(err instanceof Error ? err.message : "Resend failed"),
	});

	return (
		<div className="flex flex-col gap-4">
			<section className="border-border bg-card rounded-lg border-2">
				<header className="border-border flex flex-wrap items-center justify-between gap-3 border-b px-5 py-4">
					<h2 className="text-foreground text-base font-semibold">Users</h2>
					<div className="flex items-center gap-2">
						<Button
							variant={removeMode ? "default" : "outline"}
							size="sm"
							onClick={() => setRemoveMode((v) => !v)}
							className="h-9 gap-2"
						>
							<Minus className="h-3.5 w-3.5" />
							{removeMode ? "Done" : "Remove User"}
						</Button>
						<Button
							variant="outline"
							size="sm"
							onClick={() => setAddOpen(true)}
							className="h-9 gap-2"
						>
							<Plus className="h-3.5 w-3.5" />
							Add User
						</Button>
					</div>
				</header>

				<Table>
					<TableHeader>
						<TableRow className="hover:bg-transparent">
							<TableHead className="w-[28%]">Name</TableHead>
							<TableHead>Email</TableHead>
							<TableHead>Role</TableHead>
							{removeMode && <TableHead className="w-[60px]" />}
						</TableRow>
					</TableHeader>
					<TableBody>
						{rows.map((u) => (
							<TableRow key={u.id}>
								<TableCell>
									<div className="flex items-center gap-3">
										<Avatar>
											<AvatarFallback>{initials(u.full_name, 1)}</AvatarFallback>
										</Avatar>
										<span className="text-foreground">{u.full_name}</span>
									</div>
								</TableCell>
								<TableCell className="text-foreground">{u.email}</TableCell>
								<TableCell className="text-foreground">
									<div className="flex items-center gap-2">
										<span>{u.role}</span>
										{u.status === "pending" && (
											<>
												{/* Compact "PENDING" pill — keeps the admin aware
												    that the user hasn't activated yet without
												    needing a dedicated column. */}
												<span className="border-border text-muted-foreground rounded-sm border px-1.5 py-0.5 text-[10px] font-semibold tracking-wide uppercase">
													Pending
												</span>
												<button
													type="button"
													onClick={() => resendMutation.mutate(u.id)}
													disabled={
														resendMutation.isPending &&
														resendMutation.variables === u.id
													}
													className="text-brand hover:text-foreground text-xs underline-offset-2 hover:underline disabled:opacity-50"
												>
													{resendMutation.isPending &&
													resendMutation.variables === u.id
														? "Sending…"
														: "Resend invite"}
												</button>
											</>
										)}
									</div>
								</TableCell>
								{removeMode && (
									<TableCell className="text-right">
										<button
											type="button"
											onClick={() => setPendingRemove(u)}
											className="text-status-offline hover:bg-accent rounded-md p-2 transition-colors"
											title="Delete user"
										>
											<Trash2 className="h-4 w-4" />
										</button>
									</TableCell>
								)}
							</TableRow>
						))}
						{rows.length === 0 && (
							<TableRow>
								<TableCell
									colSpan={removeMode ? 4 : 3}
									className="text-muted-foreground py-10 text-center"
								>
									{isPending
										? "Loading users…"
										: isError
											? "Failed to load users."
											: "No users."}
								</TableCell>
							</TableRow>
						)}
					</TableBody>
				</Table>

				<TableFooter
					pageSize={pageSize}
					onPageSizeChange={(n) => {
						setPageSize(n);
						setPage(0);
					}}
					centerLabel={`Total Users: ${total}`}
					page={currentPage}
					pageCount={pageCount}
					onPageChange={setPage}
				/>
			</section>

			<AddUserFlow
				open={addOpen}
				onOpenChange={setAddOpen}
				onConfirmed={() => {
					// AddUserFlow has already POST-ed and invalidated the cache;
					// just close the dialog here.
					setAddOpen(false);
				}}
			/>

			<RemoveUserDialog
				user={pendingRemove}
				onOpenChange={(open) => !open && setPendingRemove(null)}
				onConfirm={(id) => {
					removeMutation.mutate(id);
					setPendingRemove(null);
				}}
			/>
		</div>
	);
}

/* ---------- Add User: 2-step flow (form → "invitation sent" confirmation) ---------- */

type AddDraft = { fullName: string; email: string; role: UserRole };

function AddUserFlow({
	open,
	onOpenChange,
	onConfirmed,
}: {
	open: boolean;
	onOpenChange: (v: boolean) => void;
	onConfirmed: (u: AddDraft) => void;
}) {
	const [step, setStep] = useState<"form" | "sent">("form");
	const [draft, setDraft] = useState<AddDraft>({
		fullName: "",
		email: "",
		role: "Reviewer",
	});
	const qc = useQueryClient();

	const createMutation = useMutation({
		mutationFn: () =>
			createUser({
				email: draft.email.trim(),
				full_name: draft.fullName.trim(),
				role: draft.role,
			}),
		onSuccess: () => {
			void qc.invalidateQueries({ queryKey: USERS_QUERY_KEY });
			setStep("sent");
		},
		onError: (err) =>
			toast.error(err instanceof Error ? err.message : "Create failed"),
	});

	const close = () => {
		// Reset eagerly — the previous `setTimeout(…, 200)` was a guess at
		// the dialog's exit animation and races a quick reopen click. The
		// dialog is being closed in the same tick anyway, so React batches
		// these state updates with the unmount and there's no visible
		// stale-render artifact.
		onOpenChange(false);
		setStep("form");
		setDraft({ fullName: "", email: "", role: "Reviewer" });
		createMutation.reset();
	};

	const canConfirmForm =
		draft.fullName.trim() && draft.email.trim() && !createMutation.isPending;

	return (
		<Dialog
			open={open}
			onOpenChange={(v) => (v ? onOpenChange(true) : close())}
		>
			<DialogContent
				showClose={false}
				className="max-w-md bg-dialog"
				aria-describedby={undefined}
			>
				{step === "form" ? (
					<>
						<DialogHeader>
							<DialogTitle className="text-foreground text-sm font-normal">
								To add a new user, fill in the details below. We'll email
								them an invitation link to set up their account.
							</DialogTitle>
						</DialogHeader>

						<div className="flex flex-col gap-1.5">
							<Label htmlFor="add-fullname" className="text-xs">
								Full Name
							</Label>
							<Input
								id="add-fullname"
								value={draft.fullName}
								onChange={(e) =>
									setDraft((d) => ({ ...d, fullName: e.target.value }))
								}
								placeholder="Abcdefg Cdehedk"
							/>
						</div>

						<div className="flex flex-col gap-1.5">
							<Label htmlFor="add-email" className="text-xs">
								Email
							</Label>
							<Input
								id="add-email"
								type="email"
								value={draft.email}
								onChange={(e) =>
									setDraft((d) => ({ ...d, email: e.target.value }))
								}
								placeholder="user1234@email.com"
							/>
						</div>

						<div className="flex flex-col gap-1.5">
							<Label htmlFor="add-role" className="text-xs">
								User Role
							</Label>
							<Select
								value={draft.role}
								onValueChange={(v) =>
									setDraft((d) => ({ ...d, role: v as UserRole }))
								}
							>
								<SelectTrigger id="add-role" className="bg-card h-11">
									<SelectValue />
								</SelectTrigger>
								<SelectContent>
									{USER_ROLES.map((r) => (
										<SelectItem key={r} value={r}>
											{r}
										</SelectItem>
									))}
								</SelectContent>
							</Select>
						</div>

						{/* Cancel left, Confirm right — same layout as Delete dialog. */}
						<DialogFooter className="justify-between">
							<Button variant="outline" onClick={close} className="bg-card">
								Cancel
							</Button>
							<Button
								disabled={!canConfirmForm}
								onClick={() => createMutation.mutate()}
								className="text-brand border-border hover:bg-accent hover:text-brand bg-card border"
							>
								{createMutation.isPending ? "Sending…" : "Confirm"}
							</Button>
						</DialogFooter>
					</>
				) : (
					<InvitationSentStep
						draft={draft}
						onSend={() => {
							onConfirmed(draft);
							close();
						}}
					/>
				)}
			</DialogContent>
		</Dialog>
	);
}

function InvitationSentStep({
	draft,
	onSend,
}: {
	draft: AddDraft;
	onSend: () => void;
}) {
	// Confirmation screen after `POST /api/v1/users` succeeded. The backend
	// has already emailed the invite — the admin doesn't need to copy a
	// link manually. We keep a single "Done" affordance instead of the
	// old fake "Send invitation" button.
	return (
		<>
			<DialogHeader>
				<DialogTitle className="text-foreground text-sm font-normal">
					An invitation email has been sent to {draft.email}.
				</DialogTitle>
			</DialogHeader>

			<div>
				<div className="text-foreground mb-2 text-sm font-semibold">
					User Information:
				</div>
				<dl className="space-y-1 text-sm">
					<Row label="Full Name" value={draft.fullName} />
					<Row label="Email" value={draft.email} />
					<Row label="User Role" value={draft.role} />
				</dl>
			</div>

			<p className="text-muted-foreground text-xs">
				The recipient has 15 minutes to click the magic link in their email
				to activate their account. Re-send the invite from the users list
				if it expires.
			</p>

			<DialogFooter className="justify-end">
				<Button
					onClick={onSend}
					className="text-brand border-border hover:bg-accent hover:text-brand bg-card border"
				>
					Done
				</Button>
			</DialogFooter>
		</>
	);
}

function Row({ label, value }: { label: string; value: string }) {
	return (
		<div className="text-foreground flex gap-2">
			<dt className="text-muted-foreground">{label}:</dt>
			<dd>{value}</dd>
		</div>
	);
}

/* ---------- Remove User ---------- */

function RemoveUserDialog({
	user,
	onOpenChange,
	onConfirm,
}: {
	user: ApiUser | null;
	onOpenChange: (open: boolean) => void;
	onConfirm: (id: string) => void;
}) {
	return (
		<Dialog open={!!user} onOpenChange={onOpenChange}>
			{/* Same #373D3F frame as the other confirm dialogs (Restore, Delete
			    Attack, Add User). Title centered, two equal-width buttons. */}
			<DialogContent
				showClose={false}
				className="max-w-xl gap-8 bg-dialog"
			>
				<DialogHeader>
					<DialogTitle className="text-center text-base font-normal">
						Are you sure you want to delete this user?
					</DialogTitle>
					<DialogDescription className="text-center">
						This action is irreversible.
					</DialogDescription>
				</DialogHeader>
				<DialogFooter className="flex-row gap-4 sm:justify-between">
					<Button
						variant="outline"
						onClick={() => onOpenChange(false)}
						className="bg-card flex-1"
					>
						Cancel
					</Button>
					<Button
						variant="outline"
						onClick={() => user && onConfirm(user.id)}
						className="bg-card flex-1"
					>
						Confirm
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
