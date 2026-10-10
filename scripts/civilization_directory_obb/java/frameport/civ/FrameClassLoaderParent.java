package frameport.civ;

/** Keep native findClass and its helper's nested classes in the same DexClassLoader. */
public final class FrameClassLoaderParent extends ClassLoader {
    private final ClassLoader original;
    public FrameClassLoaderParent(ClassLoader original) {
        super(original);
        this.original = original;
    }
    @Override protected Class<?> loadClass(String name, boolean resolve) throws ClassNotFoundException {
        if (name.startsWith("com.firaxis.obb.") || name.startsWith("com.firaxis.browser.")) {
            throw new ClassNotFoundException(name);
        }
        return original.loadClass(name);
    }
}
