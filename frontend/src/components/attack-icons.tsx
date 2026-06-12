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
			{/* Outer rounded square */}
			<path
				d="M5 18V6C5 5.44772 5.44772 5 6 5H18C18.5523 5 19 5.44772 19 6V18C19 18.5523 18.5523 19 18 19H6C5.44772 19 5 18.5523 5 18Z"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
			{/* Inner rounded square, concentric */}
			<rect
				x="8.5"
				y="8.5"
				width="7"
				height="7"
				rx="1"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
				fill="none"
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
				d="M8.45117 16.5488C8.45117 16.3096 8.41452 16.0789 8.35059 15.8604L6.2998 17.3994C6.14844 17.5129 5.94565 17.5317 5.77637 17.4473C5.60711 17.3626 5.50016 17.1892 5.5 17H6L5.5 16.999V14.1494C4.84972 14.2839 4.29405 14.6762 3.94336 15.2158C3.69416 15.5993 3.54884 16.0562 3.54883 16.5488C3.54883 17.9025 4.64629 19 6 19C7.35369 19 8.45117 17.9025 8.45117 16.5488ZM6.5 15.999L7.87207 14.9688C7.52503 14.5582 7.04625 14.2626 6.5 14.1494V15.999ZM9.45117 16.5488C9.45117 18.4548 7.90597 20 6 20C4.094 20 2.54883 18.4548 2.54883 16.5488C2.54884 15.8571 2.75315 15.2116 3.10449 14.6709C3.63311 13.8574 4.49652 13.2803 5.5 13.1348L5.5 6.93652C5.5 6.44874 5.49913 6.04006 5.53418 5.72461C5.56885 5.41298 5.6477 5.08179 5.89648 4.83301C6.14536 4.58426 6.47642 4.50534 6.78809 4.4707C7.10354 4.43565 7.51222 4.43652 8 4.43652L13.5498 4.43652C13.7815 3.29542 14.7905 2.43652 16 2.43652C17.2093 2.4367 18.2186 3.29547 18.4502 4.43652H20C20.276 4.43674 20.5 4.66051 20.5 4.93652C20.5 5.21253 20.276 5.43631 20 5.43652H18.4502C18.2186 6.57758 17.2093 7.43634 16 7.43652C14.7905 7.43652 13.7815 6.57763 13.5498 5.43652L8 5.43652C7.48778 5.43652 7.14549 5.43739 6.89844 5.46484C6.65484 5.49193 6.607 5.53657 6.60352 5.54004C6.60243 5.54113 6.5552 5.5852 6.52734 5.83594C6.49995 6.08295 6.5 6.42465 6.5 6.93652L6.5 13.1338C7.37332 13.2605 8.14061 13.7147 8.67383 14.3672L9.69922 13.5996C9.92014 13.4339 10.2337 13.4793 10.3994 13.7002C10.5651 13.9211 10.5207 14.2347 10.2998 14.4004L9.18945 15.2314C9.35737 15.6374 9.45116 16.0821 9.45117 16.5488Z"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}
