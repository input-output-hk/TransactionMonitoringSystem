import {
	CircularIcon,
	FakeTokenIcon,
	FrontRunningIcon,
	LargeDatumIcon,
	LargeValueIcon,
	MultipleSatIcon,
	PhishingIcon,
	SandwichIcon,
	TokenDustIcon,
} from "@/components/attack-icons";
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
	Sandwich: SandwichIcon,
	Phishing: PhishingIcon,
	Circular: CircularIcon,
	"Multiple Satisfaction": MultipleSatIcon,
	"Large Value": LargeValueIcon,
	"Large Datum": LargeDatumIcon,
	"Token Dust": TokenDustIcon,
	"Front Running": FrontRunningIcon,
	"Fake Token": FakeTokenIcon,
};
