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
 * labels are the human-readable strings from the Figma. ``description``
 * powers the per-donut info-icon tooltip and is intentionally short
 * (1-2 sentences) so it can render inline without scrolling. Wording is
 * sourced from the Polimi spec sections embedded in each scorer's
 * docstring, simplified for an operator-not-researcher audience.
 */
export const SUB_SCORE_LABELS: Record<
	AttackType,
	Array<{ key: string; label: string; description: string }>
> = {
	Phishing: [
		{
			key: "domain_suspicion",
			label: "Suspicious Domain",
			description:
				"Brand-name similarity between the URL's registrable domain and known Cardano protocol domains (jpg.store, sundaeswap, etc.). High values indicate typosquatting.",
		},
		{
			key: "social_engineering",
			label: "Social Engineering",
			description:
				"Tier classification of phishing language patterns (claim, reward, verify, urgent…) in CIP-20 messages and inline datums. Combined with a URL it strongly signals intent.",
		},
		{
			key: "url_recurrence",
			label: "Recurring URL",
			description:
				"How often this URL has appeared across recent on-chain payloads. Sustained reuse indicates an active campaign rather than a one-off message.",
		},
		{
			key: "blacklist",
			label: "URL Blacklisted",
			description:
				"Match against curated phishing-domain patterns (Cardano Foundation reports, community lists). 100% means a known-bad host.",
		},
		{
			key: "recipients",
			label: "Many Recipients",
			description:
				"Distinct recipient addresses receiving this payload. Mass-distribution patterns boost this sub-score; one-to-one messages stay low.",
		},
	],
	// "New Policy" donut (key: policy_age_inverted) intentionally omitted.
	// The fake_token scorer doesn't have an asset→first-seen index yet, so
	// it hardcodes ``policy_age_slots = 1`` (most-suspicious) for every tx.
	// Showing the donut as a permanent 100% misleads operators into
	// reading it as real signal. See docs/follow-ups/fake_token_policy_age.md
	// for the implementation plan. Reintroduce this entry once the lookup
	// table ships.
	"Fake Token": [
		{
			key: "tokenname_similarity",
			label: "Similar Token Name",
			description:
				"Levenshtein-style similarity between this asset name and a known legitimate token (HOSKY, LENFI, MIN, etc.) after Unicode normalisation. 100% = byte-identical name under a different policy ID.",
		},
		{
			key: "unicode_suspicion",
			label: "Suspicious Unicode",
			description:
				"Presence of visual homoglyphs (Cyrillic O / Greek E), zero-width characters, or mixed scripts in the asset name. Pure-ASCII names score 0%.",
		},
		{
			key: "cip25_similarity",
			label: "Metadata Match",
			description:
				"Similarity of CIP-25 metadata fields (name, ticker, image, description) against the impersonated token's official metadata. Identity-deception subsignal.",
		},
		{
			key: "recipients",
			label: "Many Recipients",
			description:
				"Distinct recipient addresses for the minted asset. Wide distribution distinguishes a phishing-style fake mint from a developer test.",
		},
	],
	Circular: [
		{
			key: "amount_similarity",
			label: "Value Preserved",
			description:
				"How tightly the ADA amount stays constant across the cycle (1 − coefficient of variation across hops). Layering preserves value minus fees; incidental hops do not.",
		},
		{
			key: "speed",
			label: "Rapid Hops",
			description:
				"Inverse of the mean inter-hop slot delta. Cycles closing within seconds are deliberate; cycles spanning hours are usually incidental address reuse.",
		},
		{
			key: "cycle_recurrence",
			label: "Repeated Cycle",
			description:
				"How many prior cycles originated from the same address within the configured recurrence window. Repeat layering patterns get amplified here.",
		},
		{
			key: "recipient_entropy_inv",
			label: "Low Address Diversity",
			description:
				"Inverted Shannon entropy of the hop addresses. A cycle reusing the same 2–3 addresses scores high; a cycle through many distinct addresses scores low.",
		},
		{
			key: "auxiliary",
			label: "Round Amounts",
			description:
				"Combined flag for round ADA amounts (multiples of 1 ADA) and tight temporal concentration. Both are stylistic markers of manual layering attempts.",
		},
	],
	Sandwich: [
		{
			key: "attacker_link",
			label: "Linked Attacker",
			description:
				"Whether the front-run (tx_A) and back-run (tx_B) come from the same address cluster. Linked = canonical MEV; unlinked = coincidental triple.",
		},
		{
			key: "swap_rate_delta",
			label: "Rate Manipulation",
			description:
				"How much worse the victim's swap rate was vs. the baseline rate at the pool. Larger negative delta = victim got squeezed harder by the front-run.",
		},
		{
			key: "price_impact",
			label: "Price Impact",
			description:
				"Per-pool baselined price impact of the front-run swap. Saturates when tx_A moved the pool reserves more than the pool typically tolerates.",
		},
		{
			key: "profit",
			label: "Profit Captured",
			description:
				"Per-pool baselined ADA profit captured by tx_B (back-run). Below the minimum-profit floor the band is capped to avoid flagging coincidental triples.",
		},
		{
			key: "attacker_recurrence",
			label: "Repeated Attacker",
			description:
				"How often this attacker cluster has executed sandwich triples in the recent window. Sustained MEV operators saturate this axis.",
		},
	],
	"Front Running": [
		{
			key: "collision_outcome",
			label: "Collision Outcome",
			description:
				"How confidently the collision is a confirmed front-run: 100% on TX_A_CONFIRMED / TX_B_CONFIRMED outcomes, lower for ambiguous or still-pending states.",
		},
		{
			key: "mempool_delta_inv",
			label: "Fast Mempool Race",
			description:
				"Inverse of the milliseconds between the two competing transactions reaching the mempool. Smaller delta = tighter race = stronger front-run signal.",
		},
		{
			key: "attacker_recurrence",
			label: "Repeated Attacker",
			description:
				"How many collisions the winning party has previously won. Repeat winners are very likely a front-running bot rather than a coincidence.",
		},
		{
			key: "structural_similarity",
			label: "Similar Structure",
			description:
				"Composite of fee similarity, TTL similarity, and shared change-address. High values indicate the two txs are deliberate near-clones, not unrelated traffic.",
		},
	],
	// We surface the asset-extraction axis (``s_extraction_assets``) rather
	// than the lovelace axis (``s_extraction_lov``) because the canonical
	// NFT-marketplace double-sat exploits drain native assets while the
	// script's lovelace position barely moves. The combined ``s_extraction``
	// donut (first slot) still covers both axes via ``max(lov, assets)``.
	"Multiple Sat": [
		{
			key: "s_extraction",
			label: "Value Extraction",
			description:
				"Max of the lovelace-extracted and native-asset-extracted signals from the script address. The canonical double-satisfaction shape drains either ADA or assets.",
		},
		{
			key: "s_inputs",
			label: "Same-Script Inputs",
			description:
				"Per-script baselined count of inputs from the same validator. Two or more under one redeemer is the structural fingerprint of the exploit.",
		},
		{
			key: "s_exunits_inv",
			label: "Low Execution Units",
			description:
				"Inverse of CPU exunits per script input. A 'lazy validator' that did almost no work per input is the strongest structural signal that one argument satisfied many checks.",
		},
		{
			key: "s_extraction_assets",
			label: "Assets Extracted",
			description:
				"Count of distinct (policy, asset-name) pairs with net flow out of the script. The NFT-marketplace double-sat shape drains assets without moving lovelace.",
		},
		{
			key: "s_recurrence",
			label: "Repeated Sender",
			description:
				"How often this sender has previously hit the same script. Sustained extraction patterns are stronger evidence than one-shot attempts.",
		},
	],
	"Large Datum": [
		{
			key: "datum_bytes",
			label: "Large Datum Size",
			description:
				"Per-script baselined byte size of the inline datum. Datums dramatically above the script's usual footprint indicate intentional bloat / DoS.",
		},
		{
			key: "datum_ratio",
			label: "High Datum Ratio",
			description:
				"Fraction of the UTxO's total bytes occupied by the datum. Near-100% means the datum is the entire UTxO; canonical Class-3 shape.",
		},
		{
			key: "value_cbor_bytes_inverted",
			label: "Small Value CBOR",
			description:
				"Inverted size of the UTxO's Value field. A lean value field paired with a fat datum is a strong fingerprint: nothing to spend, everything to store.",
		},
		{
			key: "sender_recurrence",
			label: "Repeated Sender",
			description:
				"How often the sender has previously deposited large datums at scripts. Sustained patterns are stronger evidence than a single attempt.",
		},
	],
	"Token Dust": [
		{
			key: "unique_assetclass_count",
			label: "Many Distinct Tokens",
			description:
				"Per-script baselined count of distinct (policy, asset-name) pairs in the UTxO's value. Real DoS shapes carry dozens of pairs; normal protocol UTxOs carry few.",
		},
		{
			key: "value_cbor_bytes",
			label: "Large CBOR Payload",
			description:
				"Per-script baselined byte size of the Value field. Bloat from many asset pairs balloons CBOR overhead each time the contract is consumed.",
		},
		{
			key: "lovelace_inverted",
			label: "Low ADA Amount",
			description:
				"Inverted lovelace amount. Min-ADA UTxOs paired with many tokens are the canonical dust-bomb fingerprint.",
		},
		{
			key: "sender_recurrence",
			label: "Repeated Sender",
			description:
				"How often this sender has deposited dust bundles at scripts. Repeat offenders amplify the score.",
		},
	],
	"Large Value": [
		{
			key: "quantity_digits",
			label: "Extreme Quantity",
			description:
				"Number of decimal digits in the largest asset quantity. Values near i64-max (~19 digits) are the classic overflow-attack shape.",
		},
		{
			key: "value_cbor_bytes",
			label: "Large CBOR Payload",
			description:
				"Per-script baselined Value-field byte size. Few large quantities can still inflate CBOR via variable-length integer encoding.",
		},
		{
			key: "lovelace_inverted",
			label: "Low ADA Amount",
			description:
				"Inverted lovelace amount. Min-ADA UTxOs holding huge native-asset quantities are highly atypical of normal traffic.",
		},
		{
			key: "sender_recurrence",
			label: "Repeated Sender",
			description:
				"How often this sender has minted or moved extreme-quantity assets. Sustained patterns indicate a campaign rather than a stray test.",
		},
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
		// Keep index-aligned with SUB_SCORE_LABELS["Fake Token"]: the
		// "New Policy" donut is hidden until the policy-age lookup ships
		// (see docs/follow-ups/fake_token_policy_age.md), so it must be
		// absent here too or the positional fallback in SubScores misaligns.
		subScores: [
			{ label: "Similar Token Name", percent: 65 },
			{ label: "Suspicious Unicode", percent: 72 },
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
