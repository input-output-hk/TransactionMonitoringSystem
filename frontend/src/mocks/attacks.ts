export const ATTACK_TYPES = [
	"Token Dust",
	"Large Value",
	"Large Datum",
	"Multiple Sat",
	"Front Running",
	"Sandwich",
	"Circular",
	"Fake Token",
	"Phishing",
] as const;

export type AttackType = (typeof ATTACK_TYPES)[number];

export type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

export type SubScore = { label: string; percent: number };

export type RiskAlert = {
	slug: string;
	id: string;
	fullHash: string;
	date: string;
	attackType: AttackType;
	severity: Severity;
	riskScore: number;
	feeAda: number;
	outputs: number;
	/**
	 * Backend sub-score breakdown for the winning attack class, keyed by
	 * dimension name (snake_case). Values are 0..1 normalized. Missing for
	 * mock alerts (use ATTACK_META.subScores as fallback).
	 */
	subScores?: Record<string, number>;
	/**
	 * Backend per-class raw evidence (addresses, byte counts, lists) for the
	 * winning attack class. Keys are scorer-defined; consumers should treat
	 * individual fields as optional and fall back gracefully when missing.
	 */
	evidence?: Record<string, unknown>;
};

/**
 * Per-attack-class ordered list of sub-score dimensions to display as donuts.
 * Keys match the backend's `sub_scores[max_class]` snake_case dimensions;
 * labels are the human-readable strings from the Figma.
 */
export const SUB_SCORE_LABELS: Record<
	AttackType,
	Array<{ key: string; label: string }>
> = {
	Phishing: [
		{ key: "domain_suspicion", label: "Suspicious Domain" },
		{ key: "social_engineering", label: "Social Engineering" },
		{ key: "url_recurrence", label: "Recurring URL" },
		{ key: "blacklist", label: "URL Blacklisted" },
		{ key: "recipients", label: "Many Recipients" },
	],
	"Fake Token": [
		{ key: "tokenname_similarity", label: "Similar Token Name" },
		{ key: "unicode_suspicion", label: "Suspicious Unicode" },
		{ key: "policy_age_inverted", label: "New Policy" },
		{ key: "cip25_similarity", label: "Metadata Match" },
		{ key: "recipients", label: "Many Recipients" },
	],
	Circular: [
		{ key: "amount_similarity", label: "Value Preserved" },
		{ key: "speed", label: "Rapid Hops" },
		{ key: "cycle_recurrence", label: "Repeated Cycle" },
		{ key: "recipient_entropy_inv", label: "Low Address Diversity" },
		{ key: "auxiliary", label: "Round Amounts" },
	],
	Sandwich: [
		{ key: "attacker_link", label: "Linked Attacker" },
		{ key: "swap_rate_delta", label: "Rate Manipulation" },
		{ key: "price_impact", label: "Price Impact" },
		{ key: "profit", label: "Profit Captured" },
		{ key: "attacker_recurrence", label: "Repeated Attacker" },
	],
	"Front Running": [
		{ key: "collision_detected", label: "Collision Detected" },
		{ key: "fast_mempool_race", label: "Fast Mempool Race" },
		{ key: "repeated_attacker", label: "Repeated Attacker" },
		{ key: "similar_structure", label: "Similar Structure" },
	],
	"Multiple Sat": [
		{ key: "s_extraction", label: "Full Drain Detected" },
		{ key: "s_inputs", label: "Low Redeemer Ratio" },
		{ key: "s_extraction_lov", label: "Value Extracted" },
		{ key: "s_exunits_inv", label: "Low Execution Units" },
		{ key: "s_recurrence", label: "Repeated Sender" },
	],
	"Large Datum": [
		{ key: "datum_bytes", label: "Large Datum Size" },
		{ key: "datum_ratio", label: "High Datum Ratio" },
		{ key: "value_cbor_bytes_inverted", label: "Small Value CBOR" },
		{ key: "sender_recurrence", label: "Repeated Sender" },
	],
	"Token Dust": [
		{ key: "unique_assetclass_count", label: "Many Distinct Tokens" },
		{ key: "value_cbor_bytes", label: "Large CBOR Payload" },
		{ key: "lovelace_inverted", label: "Low ADA Amount" },
		{ key: "sender_recurrence", label: "Repeated Sender" },
	],
	"Large Value": [
		{ key: "quantity_digits", label: "Extreme Quantity" },
		{ key: "value_cbor_bytes", label: "Large CBOR Payload" },
		{ key: "lovelace_inverted", label: "Low ADA Amount" },
		{ key: "sender_recurrence", label: "Repeated Sender" },
	],
};

export type LatestTx = {
	id: string;
	age: string;
	amountAda: string;
};

export type LatestBlock = {
	height: string;
	age: string;
	amountAda: string;
};

// Deterministic pseudo-hex from a numeric seed
function seededHex(seed: number, length: number): string {
	const chars = "abcdef0123456789";
	let out = "";
	let s = (seed * 9301 + 49297) % 233280;
	for (let i = 0; i < length; i++) {
		s = (s * 9301 + 49297 + i * 31) % 233280;
		out += chars[s % chars.length];
	}
	return out;
}

function displayId(seed: number) {
	return `ADWED34${seededHex(seed, 6)}...87TYHREH`;
}

function fullHash(seed: number) {
	return `${seededHex(seed * 7 + 1, 16)}...${seededHex(seed * 13 + 3, 8)}`;
}

const ROWS: {
	type: AttackType;
	sev: Severity;
	riskScore: number;
	feeAda: number;
	outputs: number;
}[] = [
	{ type: "Sandwich", sev: "LOW", riskScore: 38, feeAda: 0.23, outputs: 2 },
	{ type: "Phishing", sev: "HIGH", riskScore: 74, feeAda: 0.23, outputs: 47 },
	{
		type: "Circular",
		sev: "CRITICAL",
		riskScore: 87,
		feeAda: 0.23,
		outputs: 2,
	},
	{
		type: "Multiple Sat",
		sev: "HIGH",
		riskScore: 68,
		feeAda: 0.52,
		outputs: 2,
	},
	{
		type: "Large Value",
		sev: "MEDIUM",
		riskScore: 55,
		feeAda: 0.21,
		outputs: 1,
	},
	{ type: "Token Dust", sev: "LOW", riskScore: 32, feeAda: 0.21, outputs: 1 },
	{
		type: "Front Running",
		sev: "LOW",
		riskScore: 41,
		feeAda: 0.34,
		outputs: 2,
	},
	{
		type: "Token Dust",
		sev: "CRITICAL",
		riskScore: 91,
		feeAda: 0.21,
		outputs: 1,
	},
	{
		type: "Token Dust",
		sev: "MEDIUM",
		riskScore: 58,
		feeAda: 0.21,
		outputs: 1,
	},
	{ type: "Circular", sev: "LOW", riskScore: 36, feeAda: 0.18, outputs: 2 },
];

export const riskAlerts: RiskAlert[] = ROWS.map((r, i) => ({
	slug: `alert-${String(i + 1).padStart(3, "0")}`,
	id: displayId(i + 1),
	fullHash: fullHash(i + 1),
	date: "25.02.2026, 22:49",
	attackType: r.type,
	severity: r.sev,
	riskScore: r.riskScore,
	feeAda: r.feeAda,
	outputs: r.outputs,
}));

export function getAlertBySlug(slug: string): RiskAlert | undefined {
	return riskAlerts.find((a) => a.slug === slug);
}

// Per-type display data (description + sub-scores labels/percents)
export const ATTACK_META: Record<
	AttackType,
	{ description: string; subScores: SubScore[] }
> = {
	Sandwich: {
		description:
			"Front-runs a victim transaction and back-runs it to extract value by manipulating the AMM price.",
		subScores: [
			{ label: "Linked Attacker", percent: 65 },
			{ label: "Rate Manipulation", percent: 72 },
			{ label: "Price Impact", percent: 40 },
			{ label: "Profit Captured", percent: 40 },
			{ label: "Repeated Attacker", percent: 30 },
		],
	},
	Phishing: {
		description:
			"Transaction metadata or attached URLs attempt to lure users to credential-harvesting domains.",
		subScores: [
			{ label: "SUSPICIOUS Domain", percent: 65 },
			{ label: "Social Engineering", percent: 72 },
			{ label: "Recurring URL", percent: 40 },
			{ label: "URL Blacklisted", percent: 40 },
			{ label: "MANY RECIPIENTS", percent: 30 },
		],
	},
	Circular: {
		description:
			"Funds traverse a closed loop of addresses to obfuscate ownership and origin while preserving value.",
		subScores: [
			{ label: "Value Preserved", percent: 65 },
			{ label: "Rapid Hops", percent: 72 },
			{ label: "Repeated Cycle", percent: 40 },
			{ label: "Low Address Diversity", percent: 40 },
			{ label: "Round Amounts", percent: 30 },
		],
	},
	"Multiple Sat": {
		description:
			"Uses a single redeemer to satisfy multiple script inputs, draining a contract in one transaction.",
		subScores: [
			{ label: "Full Drain Detected", percent: 65 },
			{ label: "Low Redeemer Ratio", percent: 72 },
			{ label: "Value Extracted", percent: 40 },
			{ label: "Low Execution Units", percent: 40 },
			{ label: "Repeated Sender", percent: 30 },
		],
	},
	"Large Value": {
		description:
			"Mints a token with an extreme quantity to inflate the CBOR-encoded UTxO size — a modest but complementary bloat vector.",
		subScores: [
			{ label: "Extreme Quantity", percent: 65 },
			{ label: "Large CBOR Payload", percent: 55 },
			{ label: "Low ADA Amount", percent: 35 },
			{ label: "Repeated Sender", percent: 15 },
		],
	},
	"Large Datum": {
		description:
			"Attaches an oversized inline datum to a UTxO, making it expensive or impossible for consuming transactions to fit within the 16 KB tx size limit.",
		subScores: [
			{ label: "Large Datum Size", percent: 65 },
			{ label: "High Datum Ratio", percent: 55 },
			{ label: "Small Value CBOR", percent: 35 },
			{ label: "Repeated Sender", percent: 15 },
		],
	},
	"Token Dust": {
		description:
			"Floods a UTxO with many unique tokens, pushing its serialized size toward protocol limits and potentially blocking DApp interactions.",
		subScores: [
			{ label: "Many Distinct Tokens", percent: 65 },
			{ label: "Large CBOR Payload", percent: 55 },
			{ label: "Low ADA Amount", percent: 35 },
			{ label: "Repeated Sender", percent: 15 },
		],
	},
	"Front Running": {
		description:
			"Competes with a victim transaction in the mempool, paying a higher fee to land first and reap the reward.",
		subScores: [
			{ label: "Collision Detected", percent: 65 },
			{ label: "Fast Mempool Race", percent: 55 },
			{ label: "Repeated Attacker", percent: 35 },
			{ label: "Similar Structure", percent: 15 },
		],
	},
	"Fake Token": {
		description:
			"Mints a token mimicking a legitimate one — typically via Unicode lookalikes or metadata cloning — to deceive holders.",
		subScores: [
			{ label: "Similar Token Name", percent: 65 },
			{ label: "Suspicious Unicode", percent: 72 },
			{ label: "New Policy", percent: 40 },
			{ label: "Metadata Match", percent: 40 },
			{ label: "Many Recipients", percent: 30 },
		],
	},
};

export const latestTransactions: LatestTx[] = [
	{ id: displayId(101), age: "17 Seconds", amountAda: "0.19 ADA" },
	{ id: displayId(102), age: "25 Seconds", amountAda: "0.32 ADA" },
	{ id: displayId(103), age: "28 Seconds", amountAda: "0.28 ADA" },
	{ id: displayId(104), age: "35 Seconds", amountAda: "0.17 ADA" },
	{ id: displayId(105), age: "48 Seconds", amountAda: "0.45 ADA" },
];

export const latestBlocks: LatestBlock[] = [
	{ height: "35889543", age: "10 Seconds", amountAda: "0.19 ADA" },
	{ height: "57804321", age: "22 Seconds", amountAda: "0.32 ADA" },
	{ height: "68906787", age: "24 Seconds", amountAda: "0.28 ADA" },
	{ height: "16395038", age: "34 Seconds", amountAda: "0.17 ADA" },
	{ height: "28394758", age: "42 Seconds", amountAda: "0.45 ADA" },
];

export const criticalAlertIdLong = `dfgsdfsd4rge4resvse${seededHex(42, 20)}terge4ge4er`;

export const systemModules = [
	{ name: "Module 1", online: true },
	{ name: "Module 2", online: true },
	{ name: "Module 3", online: true },
	{ name: "Module 4", online: true },
] as const;

export const ARCHIVE_REASONS = [
	"False positive",
	"Authorized test",
	"Known internal address",
	"Duplicate alert",
	"Other",
] as const;

export type ArchiveReason = (typeof ARCHIVE_REASONS)[number];
