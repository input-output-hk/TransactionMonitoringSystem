import { describe, expect, it } from "vitest";
import { ATTACK_TYPES } from "@/lib/attacks";
import {
	attackTypeFromSnake,
	SNAKE_BY_ATTACK_TYPE,
	SUPERVISED_ATTACK_CLASS_OPTIONS,
} from "./analysis";

describe("attackTypeFromSnake", () => {
	it("maps every backend class round-trip through its snake form", () => {
		for (const t of ATTACK_TYPES) {
			expect(attackTypeFromSnake(SNAKE_BY_ATTACK_TYPE[t])).toBe(t);
		}
	});

	it("maps the divergent multiple_sat override, not a naive title-case", () => {
		// The backend column is `multiple_sat`; naive title-casing would
		// produce "Multiple Sat" and lose the icon and label.
		expect(attackTypeFromSnake("multiple_sat")).toBe("Multiple Satisfaction");
		expect(SNAKE_BY_ATTACK_TYPE["Multiple Satisfaction"]).toBe("multiple_sat");
	});

	it("maps the standard lowercase classes", () => {
		expect(attackTypeFromSnake("token_dust")).toBe("Token Dust");
		expect(attackTypeFromSnake("front_running")).toBe("Front Running");
		expect(attackTypeFromSnake("contract_anomaly")).toBe("Contract Anomaly");
	});

	it("keeps an unknown class visible via title-case fallback", () => {
		// Recall-adjacent: a class the UI doesn't know yet must still
		// render, never be dropped from the operator's view.
		expect(attackTypeFromSnake("brand_new_class")).toBe("Brand New Class");
		expect(attackTypeFromSnake("oracle")).toBe("Oracle");
	});
});

describe("SUPERVISED_ATTACK_CLASS_OPTIONS", () => {
	it("covers the nine supervised classes and excludes contract_anomaly", () => {
		expect(SUPERVISED_ATTACK_CLASS_OPTIONS).toHaveLength(9);
		const values = SUPERVISED_ATTACK_CLASS_OPTIONS.map((o) => o.value);
		expect(values).toContain("multiple_sat");
		expect(values).not.toContain("contract_anomaly");
	});
});
