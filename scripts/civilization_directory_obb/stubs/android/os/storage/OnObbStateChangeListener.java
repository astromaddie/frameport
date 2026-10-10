package android.os.storage;

/** Compile-only Android API surface. This package is excluded from the DEX. */
public abstract class OnObbStateChangeListener {
    public static final int MOUNTED = 1;
    public static final int UNMOUNTED = 2;
    public abstract void onObbStateChange(String path, int state);
}
