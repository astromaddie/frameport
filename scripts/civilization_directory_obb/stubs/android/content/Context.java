package android.content;

/** Compile-only Android API surface. This package is excluded from the DEX. */
public class Context {
    public static final String STORAGE_SERVICE = "storage";
    public ClassLoader getClassLoader() { return ClassLoader.getSystemClassLoader(); }
    public Object getSystemService(String name) { return null; }
}
