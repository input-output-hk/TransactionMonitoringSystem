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
import { addUser, removeUser, useUsers } from "@/lib/users-store";
import { copyToClipboard } from "@/lib/utils/clipboard";
import { initials } from "@/lib/utils/strings";
import { USER_ROLES, type ManagedUser, type UserRole } from "@/mocks/users";
import { Copy, Minus, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

export function UsersPage() {
	const users = useUsers();
	const [page, setPage] = useState(0);
	const [pageSize, setPageSize] = useState(10);
	const [removeMode, setRemoveMode] = useState(false);
	const [addOpen, setAddOpen] = useState(false);
	const [pendingRemove, setPendingRemove] = useState<ManagedUser | null>(null);

	const pageCount = Math.max(1, Math.ceil(users.length / pageSize));
	const currentPage = Math.min(page, pageCount - 1);
	const pageRows = useMemo(
		() => users.slice(currentPage * pageSize, (currentPage + 1) * pageSize),
		[users, currentPage, pageSize],
	);

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
						{pageRows.map((u) => (
							<TableRow key={u.id}>
								<TableCell>
									<div className="flex items-center gap-3">
										<Avatar>
											<AvatarFallback>{initials(u.fullName, 1)}</AvatarFallback>
										</Avatar>
										<span className="text-foreground">{u.fullName}</span>
									</div>
								</TableCell>
								<TableCell className="text-foreground">{u.email}</TableCell>
								<TableCell className="text-foreground">{u.role}</TableCell>
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
						{pageRows.length === 0 && (
							<TableRow>
								<TableCell
									colSpan={removeMode ? 4 : 3}
									className="text-muted-foreground py-10 text-center"
								>
									No users.
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
					centerLabel={`Total Users: ${users.length}`}
					page={currentPage}
					pageCount={pageCount}
					onPageChange={setPage}
				/>
			</section>

			<AddUserFlow
				open={addOpen}
				onOpenChange={setAddOpen}
				onConfirmed={(u) => {
					addUser(u);
					setAddOpen(false);
				}}
			/>

			<RemoveUserDialog
				user={pendingRemove}
				onOpenChange={(open) => !open && setPendingRemove(null)}
				onConfirm={(id) => {
					removeUser(id);
					setPendingRemove(null);
				}}
			/>
		</div>
	);
}

/* ---------- Add User: 2-step flow (form → invitation link) ---------- */

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
	const [step, setStep] = useState<"form" | "link">("form");
	const [draft, setDraft] = useState<AddDraft>({
		fullName: "",
		email: "",
		role: "Reviewer",
	});

	const close = () => {
		onOpenChange(false);
		// Reset only after the dialog has closed visually
		setTimeout(() => {
			setStep("form");
			setDraft({ fullName: "", email: "", role: "Reviewer" });
		}, 200);
	};

	const canConfirmForm = draft.fullName.trim() && draft.email.trim();

	return (
		<Dialog
			open={open}
			onOpenChange={(v) => (v ? onOpenChange(true) : close())}
		>
			<DialogContent
				showClose={false}
				className="max-w-md bg-white dark:bg-[#373D3F]"
				aria-describedby={undefined}
			>
				{step === "form" ? (
					<>
						<DialogHeader>
							<DialogTitle className="text-foreground text-sm font-normal">
								To add new user, please fill the sections below to generate the
								invitation link.
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
								onClick={() => setStep("link")}
								className="text-brand border-border hover:bg-accent hover:text-brand bg-card border"
							>
								Confirm
							</Button>
						</DialogFooter>
					</>
				) : (
					<InvitationLinkStep
						draft={draft}
						onCancel={close}
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

function InvitationLinkStep({
	draft,
	onCancel,
	onSend,
}: {
	draft: AddDraft;
	onCancel: () => void;
	onSend: () => void;
}) {
	const [link] = useState(
		() =>
			`/invitationlink${Math.random().toString(36).slice(2, 11)}.sghaa${Math.random().toString(36).slice(2, 6)}`,
	);

	return (
		<>
			<DialogHeader>
				<DialogTitle className="text-foreground text-sm font-normal">
					An invitation link has been successfully generated to add new user.
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

			<div className="flex flex-col gap-1.5">
				<Label htmlFor="invite-link" className="text-xs font-semibold">
					User invitation link
				</Label>
				<div className="relative">
					<Input
						id="invite-link"
						value={link}
						readOnly
						className="truncate pr-10"
					/>
					<button
						type="button"
						onClick={() =>
							copyToClipboard(link, { label: "Invitation link copied" })
						}
						className="text-muted-foreground hover:bg-accent hover:text-foreground absolute top-1/2 right-2 -translate-y-1/2 rounded-sm p-1.5"
						title="Copy link"
					>
						<Copy className="h-3.5 w-3.5" />
					</button>
				</div>
			</div>

			<DialogFooter className="justify-between">
				<Button variant="outline" onClick={onCancel} className="bg-card">
					Cancel
				</Button>
				<Button
					onClick={onSend}
					className="text-brand border-border hover:bg-accent hover:text-brand bg-card border"
				>
					Send Invitation
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
	user: ManagedUser | null;
	onOpenChange: (open: boolean) => void;
	onConfirm: (id: string) => void;
}) {
	return (
		<Dialog open={!!user} onOpenChange={onOpenChange}>
			{/* Same #373D3F frame as the other confirm dialogs (Restore, Delete
			    Attack, Add User). Title centered, two equal-width buttons. */}
			<DialogContent
				showClose={false}
				className="max-w-xl gap-8 bg-white dark:bg-[#373D3F]"
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
