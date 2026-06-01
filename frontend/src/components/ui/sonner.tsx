import { Toaster as SonnerToaster } from "sonner";
import { useTheme } from "@/components/theme-context";

export function Toaster() {
	const { theme } = useTheme();
	return (
		<SonnerToaster
			theme={theme}
			position="bottom-right"
			toastOptions={{
				classNames: {
					toast:
						"!rounded-md !border !shadow-lg !text-sm !font-medium !min-w-[280px] !justify-center",
					title: "!text-sm !font-semibold !text-center !w-full",
					success:
						"!bg-status-online/20 !border-status-online/40 !text-foreground",
					error:
						"!bg-status-offline/20 !border-status-offline/40 !text-foreground",
				},
			}}
		/>
	);
}
