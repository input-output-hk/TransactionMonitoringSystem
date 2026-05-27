/**
 * Custom attack-class icons, inlined as React components.
 *
 * Shape matches lucide-react: each accepts `className` and inherits color
 * via `stroke="currentColor"`. The provided source SVGs used
 * `stroke="white" stroke-opacity="0.5"` — fine on the dark theme but
 * invisible on light. Swapping to `currentColor` makes them follow the
 * parent's `color` (typically `text-muted-foreground`) on both themes,
 * while the 0.5 stroke-opacity is preserved for the intended muted look.
 *
 * No width/height on the root <svg> so the consumer fully controls the
 * size via Tailwind utilities (`h-4 w-4` is the most common, matching
 * lucide icons used elsewhere).
 */
type IconProps = { className?: string };

const SVG_PROPS = {
	viewBox: "0 0 23 23",
	fill: "none" as const,
	xmlns: "http://www.w3.org/2000/svg",
};

export function TokenDustIcon({ className }: IconProps) {
	return (
		<svg className={className} {...SVG_PROPS}>
			<path
				d="M20 6.57142C20 7.99158 16.4183 9.14285 12 9.14285C7.58172 9.14285 4 7.99158 4 6.57142M20 6.57142C20 5.15127 16.4183 4 12 4C7.58172 4 4 5.15127 4 6.57142M20 6.57142V11.8571M4 6.57142V11.8571M20 11.8571C20 13.2773 16.4183 14.4286 12 14.4286C7.58172 14.4286 4 13.2773 4 11.8571M20 11.8571V17.1428C20 18.563 16.4183 19.7143 12 19.7143C7.58172 19.7143 4 18.563 4 17.1428V11.8571"
				stroke="currentColor"
				strokeOpacity="0.5"
			/>
		</svg>
	);
}

export function LargeValueIcon({ className }: IconProps) {
	return (
		<svg className={className} {...SVG_PROPS}>
			<path
				d="M5 18V6C5 5.44772 5.44772 5 6 5H18C18.5523 5 19 5.44772 19 6V18C19 18.5523 18.5523 19 18 19H6C5.44772 19 5 18.5523 5 18Z"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}

export function LargeDatumIcon({ className }: IconProps) {
	return (
		<svg className={className} {...SVG_PROPS}>
			<path
				d="M16 12C16 14.2091 14.2091 16 12 16C9.79086 16 8 14.2091 8 12C8 9.79086 9.79086 8 12 8C14.2091 8 16 9.79086 16 12Z"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
			/>
			<path
				d="M12 5H18C18.5523 5 19 5.44772 19 6V12V18C19 18.5523 18.5523 19 18 19H12H6C5.44772 19 5 18.5523 5 18V12V6C5 5.44772 5.44771 5 6 5H12Z"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
			/>
		</svg>
	);
}

export function MultipleSatIcon({ className }: IconProps) {
	return (
		<svg className={className} {...SVG_PROPS}>
			<path
				d="M20 4C20 5.10457 19.1046 6 18 6C16.8954 6 16 5.10457 16 4M20 4C20 2.89543 19.1046 2 18 2C16.8954 2 16 2.89543 16 4M20 4H21M16 4C16 4 15 4 12 4C9 4 7 5.06306 7 8.06306M7 12.0631C8.10457 12.0631 9 11.1676 9 10.0631C9 8.95849 8.10457 8.06306 7 8.06306M7 12.0631C5.89543 12.0631 5 11.1676 5 10.0631M7 12.0631C7 15.0631 9 16 12 16C15 16 16 16 16 16M5 10.0631C5 9.66168 5.11824 9.28792 5.32178 8.9747C5.67838 8.42596 6.29681 8.06306 7 8.06306M5 10.0631H3M20 16C20 17.1046 19.1046 18 18 18C16.8954 18 16 17.1046 16 16M20 16C20 14.8954 19.1046 14 18 14C16.8954 14 16 14.8954 16 16M20 16H21"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}

export function FrontRunningIcon({ className }: IconProps) {
	return (
		<svg className={className} {...SVG_PROPS}>
			<path
				d="M1.49998 11.4999C1.49998 10.3954 2.39541 9.49995 3.49998 9.49995C4.60455 9.49995 5.49998 10.3954 5.49998 11.4999M1.49998 11.4999C1.49998 12.6045 2.39541 13.4999 3.49998 13.4999C4.60455 13.4999 5.49998 12.6045 5.49998 11.4999M1.49998 11.4999H0.499985M5.49998 11.4999C5.49998 11.4999 7.50092 11.4999 11.5009 11.4999C13.5009 11.4999 12.9991 11.5 12.9991 11.5M16.4991 11.4999C16.4991 10.3954 17.3945 9.49995 18.4991 9.49995C19.6037 9.49995 20.4991 10.3954 20.4991 11.4999M16.4991 11.4999C16.4991 12.6045 17.3945 13.4999 18.4991 13.4999C19.2023 13.4999 19.8207 13.137 20.1773 12.5883C20.3808 12.2751 20.4991 11.9013 20.4991 11.4999M16.4991 11.4999L12.9991 11.5M20.4991 11.4999H22.4991M12.9991 11.5L10 9M12.9991 11.5L10 14"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}

export function SandwichIcon({ className }: IconProps) {
	return (
		<svg className={className} {...SVG_PROPS}>
			<path
				d="M14.0001 11.9995H20.0005M16.0002 9.9994L14.0001 11.9995L16.0002 13.9996M9.99988 11.9995H3.99952M7.99976 9.9994L9.99988 11.9995L7.99976 13.9996M12 5.5V18.5"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}

export function CircularIcon({ className }: IconProps) {
	return (
		<svg className={className} {...SVG_PROPS}>
			<path
				d="M10.2222 4C6.65958 4.81855 4 8.04461 4 11.8996C4 12.2039 4.01656 12.5042 4.04883 12.7997M13.7778 4C17.3404 4.81855 20 8.04461 20 11.8996C20 12.2039 19.9834 12.5042 19.9512 12.7997M5.34715 16.3998C6.78229 18.5707 9.2263 20 12 20C14.7737 20 17.2177 18.5707 18.6528 16.3998"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}

export function FakeTokenIcon({ className }: IconProps) {
	return (
		<svg className={className} {...SVG_PROPS}>
			<path
				d="M20 5.57142C20 6.99158 16.4183 8.14285 12 8.14285C7.58173 8.14285 4 6.99158 4 5.57142M20 5.57142C20 4.15127 16.4183 3 12 3C7.58173 3 4 4.15127 4 5.57142M20 5.57142V9.85713C20 11.2773 16.4183 12.4286 12 12.4286M4 5.57142V9.85713C4 11.2773 7.58173 12.4286 12 12.4286M12 12.4286L12 17M10 19C10 20.1046 10.8954 21 12 21C13.1046 21 14 20.1046 14 19M10 19C10 17.8954 10.8954 17 12 17M10 19H4M14 19C14 17.8954 13.1046 17 12 17M14 19H20"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
			/>
		</svg>
	);
}

export function PhishingIcon({ className }: IconProps) {
	return (
		<svg className={className} {...SVG_PROPS}>
			<path
				d="M16 2.43652C17.3805 2.43673 18.5 3.55594 18.5 4.93652C18.5 6.31711 17.3805 7.43632 16 7.43652C14.6193 7.43652 13.5 6.31724 13.5 4.93652C13.5 3.55581 14.6193 2.43652 16 2.43652Z"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}
