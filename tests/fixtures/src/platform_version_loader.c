// Exact AArch64 Application_GetVersion zero-return export from the C-Smash OVRPort loader.
// A second export ensures the patch preserves nearby loader API behavior.
typedef unsigned long long u64;

__attribute__((visibility("default"), naked))
u64 ovr_Application_GetVersion(void) {
    __asm__("mov x0, xzr\nret");
}

__attribute__((visibility("default"), noinline))
u64 ovr_Application_GetCurrentVersion(void) {
    return 17;
}

// These exports model the loader-owned message/error API. The callback patch must hide them
// so managed dlsym calls reach the shim's queued-message wrappers.
__attribute__((visibility("default"), noinline))
void *ovr_PopMessage(void) { return (void *)0; }

__attribute__((visibility("default"), noinline))
void ovr_FreeMessage(void *message) { (void)message; }

__attribute__((visibility("default"), noinline))
unsigned int ovr_Message_GetType(const void *message) { (void)message; return 0; }

__attribute__((visibility("default"), noinline))
u64 ovr_Message_GetRequestID(const void *message) { (void)message; return 0; }

__attribute__((visibility("default"), noinline))
_Bool ovr_Message_IsError(const void *message) { (void)message; return 0; }

__attribute__((visibility("default"), noinline))
void *ovr_Message_GetError(const void *message) { (void)message; return (void *)0; }

__attribute__((visibility("default"), noinline))
int ovr_Error_GetCode(const void *error) { (void)error; return 0; }

__attribute__((visibility("default"), noinline))
int ovr_Error_GetHttpCode(const void *error) { (void)error; return 0; }

__attribute__((visibility("default"), noinline))
const char *ovr_Error_GetMessage(const void *error) { (void)error; return ""; }

__attribute__((visibility("default"), noinline))
const char *ovr_Error_GetDisplayableMessage(const void *error) { (void)error; return ""; }

// A callback-only patch must leave these APIs bound to the loader's own stubs.
__attribute__((visibility("default"), noinline))
u64 ovr_LanguagePack_GetCurrent(void) { return 0; }

__attribute__((visibility("default"), noinline))
u64 ovr_AssetFile_GetList(void) { return 0; }
