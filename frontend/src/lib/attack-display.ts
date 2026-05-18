import {
	AlertCircle,
	Banknote,
	Coins,
	Fish,
	GitFork,
	Layers,
	PackageOpen,
	Repeat,
	ScrollText,
} from "lucide-react";
import type { AttackType, Severity } from "@/mocks/attacks";

export const SEVERITY_VARIANT: Record<
	Severity,
	"low" | "medium" | "high" | "critical"
> = {
	LOW: "low",
	MEDIUM: "medium",
	HIGH: "high",
	CRITICAL: "critical",
};

export const ATTACK_ICON: Record<
	AttackType,
	React.ComponentType<{ className?: string }>
> = {
	Sandwich: PackageOpen,
	Phishing: Fish,
	Circular: Repeat,
	"Multiple Sat": Layers,
	"Large Value": Banknote,
	"Large Datum": ScrollText,
	"Token Dust": Coins,
	"Front Running": GitFork,
	"Fake Token": AlertCircle,
};
