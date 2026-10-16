"""Version-independent service build capabilities and variant declarations."""
from . import RigError


def architectures(value, where):
    if not isinstance(value, list) or not value or any(a not in ('arm64', 'amd64') for a in value):
        raise RigError(f"{where}: architectures must be a nonempty list of arm64/amd64")
    return list(dict.fromkeys(value))


def parse_contract(data, path):
    build = data.get('build')
    build = build if isinstance(build, dict) else {}
    arches = architectures(build['architectures'], path) if 'architectures' in build else []
    capabilities = build.get('capabilities', {})
    if (not isinstance(capabilities, dict) or set(capabilities) - {'no_cache', 'platforms'}
            or any(not isinstance(v, bool) for v in capabilities.values())):
        raise RigError(f"{path}: build.capabilities supports boolean no_cache/platforms")
    variants = build.get('variants', {})
    if not isinstance(variants, dict) or set(variants) - set(build.get('platforms', [])):
        raise RigError(f"{path}: build.variants must name entries from build.platforms")
    for name, spec in variants.items():
        if not isinstance(spec, dict) or set(spec) - {'architectures', 'images'}:
            raise RigError(f"{path}: build.variants.{name} supports architectures/images")
        if 'architectures' in spec:
            architectures(spec['architectures'], f'{path}:{name}')
        if 'images' in spec and (not isinstance(spec['images'], list) or not spec['images']
                                or any(i not in build.get('images', []) for i in spec['images'])):
            raise RigError(f"{path}: variant images must be a nonempty subset of build.images")
    images = data.get('images') or {}
    env = images.get('override_env', {}) if isinstance(images, dict) else {}
    import re
    from .manifest import RIG_OWNED_ENV
    if not isinstance(env, dict) or any(
            key not in build.get('images', []) or not isinstance(value, str)
            or not re.fullmatch(r'[A-Z][A-Z0-9_]*', value) or value in RIG_OWNED_ENV
            for key, value in env.items()):
        raise RigError(f"{path}: images.override_env must map built images to service-owned env names")
    return dict(build_architectures=arches, build_variants=variants,
                build_no_cache=capabilities.get('no_cache'),
                build_multiarch=capabilities.get('platforms', False), image_env=env)


def variant_spec(desc, variant):
    spec = desc.build_variants.get(variant, {})
    return (spec.get('architectures', desc.build_architectures),
            spec.get('images', desc.build_images))
