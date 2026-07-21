/** Shared feature-set picker, used by the clustering and anomaly run controls
 *  so both stay in sync with the available feature sets. */
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import { FEATURE_SETS, type FeatureSet } from "@/lib/api/clustering";

type Props = {
	value: FeatureSet;
	onChange: (value: FeatureSet) => void;
	id?: string;
	className?: string;
};

export function FeatureSetSelect({ value, onChange, id, className }: Props) {
	return (
		<Select value={value} onValueChange={(v) => onChange(v as FeatureSet)}>
			<SelectTrigger id={id} className={className}>
				<SelectValue />
			</SelectTrigger>
			<SelectContent>
				{FEATURE_SETS.map((fs) => (
					<SelectItem key={fs} value={fs}>
						{fs}
					</SelectItem>
				))}
			</SelectContent>
		</Select>
	);
}
